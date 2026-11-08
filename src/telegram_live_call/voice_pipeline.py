"""Low-risk voice pipeline for the Telegram live-call prototype.

Scope: pure transport/serialization code for STT (Deepgram streaming), TTS
(ElevenLabs) and reply generation (OpenRouter). No credential loading, no
Telegram/audio-device handling, no persistence, no tool calls, no writes —
the caller owns auth, lifecycle, call safety and real-provider verification.

Interface (caller passes every key; nothing is read from the environment here)
--------------------------------------------------------------------------------
    pipe = VoicePipeline(
        deepgram_api_key=str, elevenlabs_api_key=str, openrouter_api_key=str,
        on_transcript=async (text: str) -> None,   # final endpointed sentence only
        on_interim=None,                            # (text) -> None, for barge-in
        on_speech_started=None,                     # () -> None, VAD barge-in
        system_context=str = "",                    # system prompt from the caller
        chat_model=str = DEFAULT_CHAT_MODEL,        # override with your preferred model
        voice_id=str,                              # explicitly configure your voice
        tts_model=str = ELEVENLABS_MODEL_ID,
        output_format=str = ELEVENLABS_OUTPUT_FORMAT,
        max_speech_chars=int = 400,                 # per-request TTS cap
        speech_budget_chars=int | None = 4000,      # per-call aggregate cap
        deepgram_url=..., tts_base_url=..., openrouter_url=...,
        connect_timeout=10.0, request_timeout=20.0, ws_receive_timeout=None,
        queue_maxsize=50,
    )

    await pipe.start()            # open Deepgram WS + start send/receive tasks
    await pipe.feed_audio(pcm)    # 48 kHz / 16-bit / mono PCM bytes -> frames
    pcm = await pipe.synthesize(text)      # ElevenLabs -> raw pcm bytes (even)
    reply = await pipe.respond(history)    # OpenRouter -> short Russian text
    await pipe.stop()             # cancel tasks, close WS (safe if not started)
    await pipe.close()            # close aiohttp session (idempotent, call last)

    await pipe.close() alone is enough when only synthesize/respond were used.

Probe attributes: pending_frames, dropped_frames, speech_chars_used,
last_error (sanitized VoicePipelineError | None), reset_speech_budget().
Provider failures raise VoicePipelineError with provider/status/message where
the message is redacted (keys replaced by '***') and truncated.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from typing import Any, Awaitable, Callable, Optional

import aiohttp

LOG = logging.getLogger(__name__)

# --- audio format: Telegram/ntgcalls PCM in and out ------------------------- #
SAMPLE_RATE = 48000
PCM_CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit signed little-endian
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000 * SAMPLE_WIDTH  # 1920 bytes

# --- providers ------------------------------------------------------------- #
DEEPGRAM_URL = "wss://api.deepgram.com/v1/listen"
DEEPGRAM_MODEL = "nova-3"
DEEPGRAM_LANGUAGE = "multi"
ENDPOINTING_MS = 400
ELEVENLABS_BASE_URL = "https://api.elevenlabs.io/v1/text-to-speech"

ELEVENLABS_MODEL_ID = "eleven_v4"
ELEVENLABS_OUTPUT_FORMAT = "pcm_48000"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_CHAT_MODEL = "openai/gpt-4.1-mini"

Callback = Callable[..., Optional[Awaitable[None]]]


class VoicePipelineError(RuntimeError):
    """Sanitized provider error (never contains API keys)."""

    def __init__(self, provider: str, message: str, status: Optional[int] = None):
        self.provider = provider
        self.message = message
        self.status = status
        prefix = f"{provider} error" + (f" ({status})" if status is not None else "")
        super().__init__(f"{prefix}: {message}")

    def __repr__(self) -> str:  # keep keys out of tracebacks
        return f"VoicePipelineError(provider={self.provider!r}, status={self.status!r}, message={self.message!r})"


def split_pcm_frames(data: bytes, frame_bytes: int = FRAME_BYTES) -> tuple[list[bytes], bytes]:
    """Split 16-bit PCM into whole frames; return (frames, leftover bytes)."""
    if frame_bytes <= 0:
        raise ValueError("frame_bytes must be positive")
    buf = bytes(data or b"")
    count = len(buf) // frame_bytes
    frames = [buf[i * frame_bytes:(i + 1) * frame_bytes] for i in range(count)]
    return frames, buf[count * frame_bytes:]


def limit_speech_chars(text: str, max_chars: int) -> str:
    """Trim text to max_chars, preferring the last sentence end inside the window."""
    if max_chars < 1:
        raise ValueError("max_chars must be >= 1")
    clean = (text or "").strip()
    if len(clean) <= max_chars:
        return clean
    window = clean[:max_chars]
    cut = max(window.rfind(ch) for ch in ".!?…")
    if cut > 0:
        window = window[: cut + 1]
    return window.strip()


def parse_message(raw: Any) -> Optional[dict]:
    """Parse a Deepgram websocket payload (str/bytes/dict) into a dict or None."""
    if raw is None:
        return None
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8", "replace")
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


class TranscriptAggregator:
    """Turn Deepgram result stream into endpointed sentences.

    Interim results are ignored for text output but kept in ``interim`` so the
    caller can barge in. Final segments are joined until ``speech_final``
    (endpointing=400 ms) closes the utterance; only that joined sentence is
    returned.
    """

    def __init__(self) -> None:
        self._parts: list[str] = []
        self.interim = ""

    def feed_message(self, raw: Any) -> Optional[str]:
        return self.feed(parse_message(raw))

    def feed(self, payload: Optional[dict]) -> Optional[str]:
        if not isinstance(payload, dict) or payload.get("type") not in (None, "Results"):
            return None
        alternatives = (payload.get("channel") or {}).get("alternatives") or []
        first = alternatives[0] if alternatives and isinstance(alternatives[0], dict) else {}
        transcript = (first.get("transcript") or "").strip()
        if not payload.get("is_final"):
            if transcript:
                self.interim = transcript
            return None
        self.interim = ""
        if transcript:
            last = self._parts[-1] if self._parts else None
            if last == transcript:
                pass  # duplicate delivery of the same chunk
            elif last is not None and transcript.startswith(last):
                self._parts[-1] = transcript  # cumulative re-send of the same chunk
            else:
                self._parts.append(transcript)
        if not payload.get("speech_final") or not self._parts:
            return None
        text = " ".join(self._parts).strip()
        self._parts.clear()
        return text or None


class VoicePipeline:
    """Deepgram STT + ElevenLabs TTS + OpenRouter replies over aiohttp."""

    def __init__(
        self,
        *,
        deepgram_api_key: str,
        elevenlabs_api_key: str,
        openrouter_api_key: str,
        on_transcript: Callback,
        on_interim: Optional[Callback] = None,
        on_speech_started: Optional[Callback] = None,
        system_context: str = "",
        chat_model: str = DEFAULT_CHAT_MODEL,
        voice_id: str = "",
        tts_model: str = ELEVENLABS_MODEL_ID,
        output_format: str = ELEVENLABS_OUTPUT_FORMAT,
        max_speech_chars: int = 400,
        speech_budget_chars: Optional[int] = 4000,
        deepgram_url: str = DEEPGRAM_URL,
        tts_base_url: str = ELEVENLABS_BASE_URL,
        openrouter_url: str = OPENROUTER_URL,
        connect_timeout: float = 10.0,
        request_timeout: float = 20.0,
        ws_receive_timeout: Optional[float] = None,
        queue_maxsize: int = 50,
    ) -> None:
        for name, value in (
            ("deepgram_api_key", deepgram_api_key),
            ("elevenlabs_api_key", elevenlabs_api_key),
            ("openrouter_api_key", openrouter_api_key),
            ("voice_id", voice_id),
        ):
            if not value or not str(value).strip():
                raise ValueError(f"{name} is required")
        if not callable(on_transcript):
            raise ValueError("on_transcript must be callable")
        if max_speech_chars < 1:
            raise ValueError("max_speech_chars must be >= 1")
        if queue_maxsize < 1:
            raise ValueError("queue_maxsize must be >= 1")

        self._deepgram_api_key = str(deepgram_api_key)
        self._elevenlabs_api_key = str(elevenlabs_api_key)
        self._openrouter_api_key = str(openrouter_api_key)
        self._secrets = (self._deepgram_api_key, self._elevenlabs_api_key, self._openrouter_api_key)

        self._on_transcript = on_transcript
        self._on_interim = on_interim
        self._on_speech_started = on_speech_started
        self._system_context = system_context or ""
        self._chat_model = chat_model
        self._voice_id = voice_id
        self._tts_model = tts_model
        self._output_format = output_format
        self._max_speech_chars = max_speech_chars
        self._speech_budget_chars = speech_budget_chars
        self._deepgram_url = deepgram_url
        self._tts_base_url = tts_base_url.rstrip("/")
        self._openrouter_url = openrouter_url
        self._connect_timeout = connect_timeout
        self._request_timeout = request_timeout
        self._ws_receive_timeout = ws_receive_timeout

        self._aggregator = TranscriptAggregator()
        self._out: asyncio.Queue[bytes] = asyncio.Queue(maxsize=queue_maxsize)
        self._pcm_remainder = bytearray()
        self._dropped_frames = 0
        self._speech_chars_used = 0
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws = None
        self._tasks: list[asyncio.Task] = []
        self.last_error: Optional[VoicePipelineError] = None

    # ------------------------------------------------------------------ info #
    def __repr__(self) -> str:
        return (
            f"VoicePipeline(chat_model={self._chat_model!r}, voice_id={self._voice_id!r}, "
            f"tts_model={self._tts_model!r}, output_format={self._output_format!r}, "
            f"streaming={self._ws is not None})"
        )

    @property
    def chat_model(self) -> str:
        return self._chat_model

    @property
    def tts_model(self) -> str:
        return self._tts_model

    @property
    def output_format(self) -> str:
        return self._output_format

    @property
    def voice_id(self) -> str:
        return self._voice_id

    @property
    def pending_frames(self) -> int:
        return self._out.qsize()

    @property
    def dropped_frames(self) -> int:
        return self._dropped_frames

    @property
    def speech_chars_used(self) -> int:
        return self._speech_chars_used

    @property
    def streaming_closed(self) -> bool:
        """Observe a clean provider disconnect as well as an error disconnect."""
        return self._ws is not None and self._ws.closed

    def reset_speech_budget(self) -> None:
        self._speech_chars_used = 0

    def deepgram_params(self) -> dict[str, Any]:
        return {
            "model": DEEPGRAM_MODEL,
            "language": DEEPGRAM_LANGUAGE,
            "encoding": "linear16",
            "sample_rate": SAMPLE_RATE,
            "channels": PCM_CHANNELS,
            "interim_results": "true",
            "endpointing": ENDPOINTING_MS,
            "vad_events": "true",
        }

    # -------------------------------------------------------------- helpers #
    def _sanitize(self, text: Any, limit: int = 300) -> str:
        out = str(text or "").strip()
        for secret in self._secrets:
            if secret:
                out = out.replace(secret, "***")
        return out if len(out) <= limit else out[:limit] + "…"

    @staticmethod
    def _is_timeout(exc: BaseException) -> bool:
        return isinstance(exc, (TimeoutError, asyncio.TimeoutError))

    def _wrap_error(self, provider: str, exc: BaseException) -> VoicePipelineError:
        if isinstance(exc, VoicePipelineError):
            return exc
        if self._is_timeout(exc):
            return VoicePipelineError(provider, "request timed out")
        return VoicePipelineError(provider, f"network error: {type(exc).__name__}: {self._sanitize(exc)}")

    async def _emit(self, callback: Optional[Callback], *args: Any) -> None:
        if callback is None:
            return
        try:
            result = callback(*args)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # caller bug must not kill the stream
            LOG.warning("voice pipeline callback failed: %s", self._sanitize(exc))

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(
                total=self._request_timeout, connect=self._connect_timeout,
                sock_read=self._request_timeout,
            )
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    # ------------------------------------------------------------- streaming #
    async def start(self) -> None:
        """Open the Deepgram stream and start the sender/receiver tasks."""
        if self._ws is not None:
            return
        session = self._ensure_session()
        try:
            self._ws = await session.ws_connect(
                self._deepgram_url,
                params=self.deepgram_params(),
                headers={"Authorization": f"Token {self._deepgram_api_key}"},
                timeout=aiohttp.ClientWSTimeout(
                    ws_receive=self._ws_receive_timeout, ws_close=self._connect_timeout
                ),
                max_msg_size=1 << 20,
            )
        except Exception as exc:
            raise self._wrap_error("deepgram", exc) from None
        self._tasks = [
            asyncio.create_task(self._sender_loop(), name="voice-pipeline-sender"),
            asyncio.create_task(self._receiver_loop(), name="voice-pipeline-receiver"),
        ]

    async def feed_audio(self, pcm: bytes) -> int:
        """Queue 48 kHz/16-bit/mono PCM; returns the number of 20 ms frames queued."""
        if not pcm:
            return 0
        if len(pcm) % SAMPLE_WIDTH:
            raise ValueError("PCM must be 16-bit sample aligned (even byte count)")
        self._pcm_remainder += pcm
        frames, rest = split_pcm_frames(self._pcm_remainder)
        self._pcm_remainder = bytearray(rest)
        queued = 0
        for frame in frames:
            try:
                self._out.put_nowait(frame)
                queued += 1
            except asyncio.QueueFull:  # never block the caller's audio feeder
                self._dropped_frames += 1
        return queued

    async def _sender_loop(self) -> None:
        ws = self._ws
        try:
            while ws is not None and not ws.closed:
                frame = await self._out.get()
                await ws.send_bytes(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = self._wrap_error("deepgram", exc)
            LOG.warning("deepgram send loop stopped: %s", self.last_error)

    async def _receiver_loop(self) -> None:
        ws = self._ws
        try:
            async for msg in ws:
                if msg.type is aiohttp.WSMsgType.ERROR:
                    raise ws.exception() or ConnectionError("websocket error")
                if msg.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                                    aiohttp.WSMsgType.CLOSING):
                        break
                    continue
                await self._handle_message(msg.data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = self._wrap_error("deepgram", exc)
            LOG.warning("deepgram receive loop stopped: %s", self.last_error)

    async def _handle_message(self, raw: Any) -> None:
        payload = parse_message(raw)
        if payload is None:
            return
        if payload.get("type") == "SpeechStarted":
            await self._emit(self._on_speech_started)
            return
        text = self._aggregator.feed(payload)
        if self._aggregator.interim:
            await self._emit(self._on_interim, self._aggregator.interim)
        if text:
            await self._emit(self._on_transcript, text)

    async def stop(self) -> None:
        """Cancel streaming tasks and close the websocket (safe if not started)."""
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        await self.close()

    async def close(self) -> None:
        """Close the HTTP session. Idempotent; call after stop()."""
        session, self._session = self._session, None
        if session is not None and not session.closed:
            try:
                await session.close()
            except Exception:
                pass

    # ------------------------------------------------------------------ TTS #
    async def synthesize(self, text: str) -> bytes:
        """ElevenLabs TTS -> raw 16-bit PCM bytes (validated even length)."""
        spoken = limit_speech_chars(text, self._max_speech_chars)
        if not spoken:
            raise ValueError("text is empty after trimming")
        if (self._speech_budget_chars is not None
                and self._speech_chars_used + len(spoken) > self._speech_budget_chars):
            raise VoicePipelineError(
                "elevenlabs",
                f"speech char budget exhausted (used {self._speech_chars_used} "
                f"of {self._speech_budget_chars})",
            )
        url = f"{self._tts_base_url}/{self._voice_id}"
        session = self._ensure_session()
        try:
            async with session.post(
                url,
                json={"text": spoken, "model_id": self._tts_model},
                params={"output_format": self._output_format},
                headers={"xi-api-key": self._elevenlabs_api_key,
                         "Accept": "application/octet-stream"},
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise VoicePipelineError(
                        "elevenlabs", f"HTTP {resp.status}: {self._sanitize(body)}", resp.status
                    )
                pcm = await resp.read()
        except VoicePipelineError:
            raise
        except Exception as exc:
            raise self._wrap_error("elevenlabs", exc) from None

        if not pcm:
            raise VoicePipelineError("elevenlabs", "empty pcm response")
        if len(pcm) % SAMPLE_WIDTH:
            raise VoicePipelineError(
                "elevenlabs", f"odd pcm byte count {len(pcm)}; 16-bit samples expected"
            )
        self._speech_chars_used += len(spoken)
        return pcm

    # ------------------------------------------------------------- OpenRouter #
    async def respond(self, history: list[dict], *, system_context: Optional[str] = None) -> str:
        """Short spoken Russian reply from OpenRouter; no tools, no side effects."""
        if not isinstance(history, list):
            raise ValueError("history must be a list of chat messages")
        context = self._system_context if system_context is None else system_context
        messages: list[dict] = []
        if context:
            messages.append({"role": "system", "content": context})
        messages.extend(history)
        payload = {
            "model": self._chat_model,
            "messages": messages,
            "max_tokens": 200,
            "temperature": 0.6,
        }
        session = self._ensure_session()
        try:
            async with session.post(
                self._openrouter_url,
                json=payload,
                headers={"Authorization": f"Bearer {self._openrouter_api_key}",
                         "Content-Type": "application/json"},
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise VoicePipelineError(
                        "openrouter", f"HTTP {resp.status}: {self._sanitize(body)}", resp.status
                    )
                data = await resp.json(content_type=None)
        except VoicePipelineError:
            raise
        except Exception as exc:
            raise self._wrap_error("openrouter", exc) from None

        try:
            content = (data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError, AttributeError):
            raise VoicePipelineError(
                "openrouter", f"malformed completion: {self._sanitize(json.dumps(data)[:200])}"
            ) from None
        if not content:
            raise VoicePipelineError("openrouter", "empty completion")
        return content
