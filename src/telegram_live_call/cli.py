"""Owner-only Telegram voice-call bridge: one configured owner, one bounded call.

Commands (``--env-file`` goes *before* the command name)::

    telegram-live-call --env-file .env check              # read-only verification
    telegram-live-call --env-file .env login              # interactive, dedicated session
    telegram-live-call --env-file .env prepare            # pre-generate the greeting PCM
    telegram-live-call --env-file .env call --confirm     # exactly one call, hard limit
    telegram-live-call --env-file .env call --confirm --save-transcript

Design limits, all deliberate:

* No daemon, no retry loop, no incoming-call answering, no scheduler. One process
  performs one call and exits.
* ``call`` refuses to run without ``--confirm``: nothing is loaded, connected or
  dialled before that flag is present.
* Only ``CALL_OWNER_ID`` can be called and only ``CALL_SOURCE_ID`` may be the
  signed-in account (``call_policy.CallPolicy``, fail closed).
* The dedicated session file is locked with ``fcntl`` (Linux/macOS) *before* any
  ``connect()``/login, so two processes can never share it.
* Call duration is capped by ``CALL_MAX_SECONDS`` (default 60, max 300) and the
  limit covers ringing; ``call_session.bounded_call`` hangs up in ``finally``.
  SIGINT/SIGTERM cancel gracefully so the hangup still runs.
* The reply model has no tools and cannot write anything.

Privacy: ``CONTEXT_FILE`` (when set) and the conversation text are sent to the
configured reply model on OpenRouter; microphone audio is streamed to Deepgram;
spoken replies are synthesized by ElevenLabs. Nothing else leaves the machine.
Speech text is printed only with ``--save-transcript``; that transcript file is
created with mode 0600 in the private state directory.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from .call_policy import CallPolicy
from .session import (
    SessionBusy,
    SessionLock,
    build_client,
    ensure_dir,
    ensure_private,
    next_state_file,
    session_file_path,
)
from .settings import PROVIDER_SERVICES, Settings, SettingsError, from_env

__all__ = [
    "CallStack",
    "CliError",
    "EventLog",
    "build_policy",
    "install_signal_handlers",
    "main",
    "parse_args",
    "resolve_owner",
    "run_call",
    "run_check",
    "run_login",
]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

#: Per-utterance TTS cap and per-call speech budget (hard ceiling, ~2k characters).
MAX_SPEECH_CHARS = 400
SPEECH_BUDGET_CHARS = 2000
REQUEST_TIMEOUT = 20.0
QUEUE_MAXSIZE = 10
KEEPALIVE_INTERVAL = 0.1
SILENCE_FRAME = b"\0" * 9600
HISTORY_LIMIT = 20
FAREWELL_PHRASES = ("давай закончим", "заканчиваем", "отбой", "до свидания")

EPILOG = """\
privacy: CONTEXT_FILE and conversation text go to the reply model (OpenRouter),
microphone audio goes to Deepgram, replies are synthesized by ElevenLabs.
speech text is logged only with --save-transcript (mode 0600, state directory).
"""


class CliError(RuntimeError):
    """Operational failure with a stable machine-readable reason."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


# --------------------------------------------------------------------- events #
class EventLog:
    """JSON lines on stdout with secret redaction and an optional private transcript."""

    def __init__(
        self,
        stream: Any = None,
        *,
        secrets: Sequence[str] = (),
        transcript_path: Optional[Path] = None,
    ) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._secrets = tuple(secret for secret in secrets if secret)
        self._started = time.monotonic()
        self.transcript_path = Path(transcript_path) if transcript_path is not None else None
        self._file = None
        if self.transcript_path is not None:
            ensure_dir(self.transcript_path.parent)
            fd = os.open(
                self.transcript_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
            )
            self._file = os.fdopen(fd, "a", encoding="utf-8")
            ensure_private(self.transcript_path)

    @property
    def saves_transcript(self) -> bool:
        return self._file is not None

    def event(self, kind: str, **data: Any) -> dict:
        record: dict[str, Any] = {
            "event": kind,
            "elapsed": round(time.monotonic() - self._started, 3),
        }
        for key, value in data.items():
            if key == "text":
                text = self._redact(str(value))
                record["text_chars"] = len(text)
                if self.saves_transcript:
                    record["text"] = text
                continue
            record[key] = self._redact(value)
        line = json.dumps(record, ensure_ascii=False, default=str)
        print(line, file=self._stream, flush=True)
        if self._file is not None:
            self._file.write(line + "\n")
            self._file.flush()
        return record

    def _redact(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in self._secrets:
                value = value.replace(secret, "[redacted]")
            return value
        if isinstance(value, (list, tuple)):
            return [self._redact(item) for item in value]
        if isinstance(value, dict):
            return {key: self._redact(item) for key, item in value.items()}
        return value

    def close(self) -> None:
        handle, self._file = self._file, None
        if handle is not None:
            with contextlib.suppress(Exception):
                handle.close()


def settings_secrets(settings: Settings) -> tuple[str, ...]:
    """Values that must never reach stdout, a log file or an error message."""
    return (
        settings.telegram_api_hash,
        settings.deepgram_api_key,
        settings.elevenlabs_api_key,
        settings.openrouter_api_key,
        settings.elevenlabs_voice_id,
    )


# ------------------------------------------------------------------ arguments #
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="telegram-live-call",
        description="Owner-only Telegram voice-call bridge (single bounded call).",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        metavar="PATH",
        help="explicit dotenv file; takes precedence over ambient environment variables",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="read-only: verify session, source account and owner")
    commands.add_parser("login", help="interactive sign-in for the dedicated session")
    commands.add_parser("prepare", help="pre-generate the greeting PCM (no call)")
    call = commands.add_parser("call", help="place exactly one call to the configured owner")
    call.add_argument(
        "--confirm",
        action="store_true",
        help="required acknowledgement: this dials the owner's Telegram account",
    )
    call.add_argument(
        "--save-transcript",
        action="store_true",
        help="also write speech text to a mode-0600 JSONL file in the state directory",
    )
    return parser.parse_args(argv)


def load_settings(env_file: Optional[Path] = None) -> Settings:
    """Indirection point so tests and embedders can supply settings directly."""
    return from_env(env_file)


def build_policy(settings: Settings) -> CallPolicy:
    return CallPolicy(owner_id=settings.call_owner_id, source_id=settings.call_source_id)


# ------------------------------------------------------------------ session IO #
def acquire_session_lock(settings: Settings) -> SessionLock:
    """Exclusive fcntl lock over the dedicated session; taken before any connect."""
    return SessionLock(settings.session_path).acquire()


@contextlib.asynccontextmanager
async def dedicated_client(settings: Settings, *, log: EventLog):
    lock = acquire_session_lock(settings)
    app = None
    try:
        app = build_client(settings)
        await app.connect()
        yield app
    finally:
        if app is not None:
            with contextlib.suppress(Exception):
                await app.disconnect()
        lock.release()


def transcript_path(settings: Settings) -> Path:
    ensure_dir(settings.state_dir)
    return next_state_file(settings.state_dir, "call")


@contextlib.contextmanager
def install_signal_handlers(stop: asyncio.Event, *, loop=None, log: Optional[EventLog] = None):
    """Turn SIGINT/SIGTERM into a graceful stop so the hangup path still runs."""
    loop = loop if loop is not None else asyncio.get_running_loop()
    installed: list[int] = []

    def request_stop(*_args: Any) -> None:
        stop.set()
        if log is not None:
            log.event("signal", reason="stop_requested")

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except (NotImplementedError, RuntimeError, AttributeError):
            continue
        installed.append(sig)
    try:
        yield installed
    finally:
        for sig in installed:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(sig)


# ------------------------------------------------------------------- accounts #
def _peer_user(user_id: int) -> Any:
    from telethon.tl.types import PeerUser

    return PeerUser(user_id)


async def _try_entity(app: Any, target: Any) -> Any:
    try:
        return await app.get_entity(target)
    except Exception:
        return None


async def resolve_owner(
    app: Any,
    settings: Settings,
    policy: CallPolicy,
    *,
    peer_user: Optional[Any] = None,
    log: Optional[EventLog] = None,
) -> Any:
    """Resolve the configured owner to a Telegram user and re-verify the ID."""
    peer = peer_user if peer_user is not None else _peer_user
    target = settings.call_owner_id
    entity = await _try_entity(app, peer(target))
    if entity is None:
        if log is not None:
            log.event("owner_cache_miss", target=target)
        # Telethon needs a cached access hash: fill the cache once, then retry.
        with contextlib.suppress(Exception):
            await app.get_dialogs(limit=None)
        entity = await _try_entity(app, peer(target))
    if entity is None and settings.call_owner_username:
        entity = await _try_entity(app, settings.call_owner_username)
    if entity is None:
        raise CliError(
            "owner_unresolved",
            "the configured owner ID is not resolvable from this account; "
            "write to the owner once from the assistant account, then retry",
        )
    resolved = getattr(entity, "id", None)
    policy.authorize_target(resolved)
    return entity


async def run_check(
    app: Any,
    settings: Settings,
    policy: CallPolicy,
    *,
    log: EventLog,
    peer_user: Optional[Any] = None,
) -> dict:
    """Read-only verification: session, source account, owner. Never dials."""
    if not await app.is_user_authorized():
        raise CliError(
            "not_authorized",
            "the dedicated session is not signed in; run: telegram-live-call login",
        )
    me = await app.get_me()
    policy.authorize_source(getattr(me, "id", None))
    owner = await resolve_owner(app, settings, policy, peer_user=peer_user, log=log)
    record = {
        "ok": True,
        "source_id": getattr(me, "id"),
        "owner_id": getattr(owner, "id"),
        "dialed": False,
        "session_file": str(session_file_path(settings.session_path)),
        "max_seconds": settings.call_max_seconds,
        "chat_model": settings.chat_model,
        "tts_model": settings.tts_model,
        "context": "file" if settings.context_file else "neutral",
        "providers": list(PROVIDER_SERVICES),
    }
    log.event("check", **record)
    return record


async def run_login(app: Any, settings: Settings, policy: CallPolicy, *, log: EventLog) -> dict:
    """Interactive sign-in for the dedicated session; the session file becomes 0600."""
    if not await app.is_user_authorized():
        log.event(
            "login_required",
            hint="interactive Telegram sign-in: phone number, login code, optional 2FA password",
        )
        await app.start()
    me = await app.get_me()
    policy.authorize_source(getattr(me, "id", None))
    session_file = session_file_path(settings.session_path)
    record = {
        "ok": True,
        "source_id": getattr(me, "id"),
        "owner_id": settings.call_owner_id,
        "session_file": str(session_file),
        "session_file_private": ensure_private(session_file),
    }
    log.event("login", **record)
    return record


# -------------------------------------------------------------------- greeting #
def _voice_pipeline_class() -> Any:
    from .voice_pipeline import VoicePipeline

    return VoicePipeline


def make_pipeline(
    settings: Settings,
    *,
    factory: Any = None,
    on_transcript: Any,
    on_interim: Any = None,
    on_speech_started: Any = None,
) -> Any:
    """Build the streaming pipeline; the voice is always the configured one."""
    build = factory if factory is not None else _voice_pipeline_class()
    return build(
        deepgram_api_key=settings.deepgram_api_key,
        elevenlabs_api_key=settings.elevenlabs_api_key,
        openrouter_api_key=settings.openrouter_api_key,
        on_transcript=on_transcript,
        on_interim=on_interim,
        on_speech_started=on_speech_started,
        system_context=settings.system_context(),
        chat_model=settings.chat_model,
        voice_id=settings.elevenlabs_voice_id,
        tts_model=settings.tts_model,
        max_speech_chars=MAX_SPEECH_CHARS,
        speech_budget_chars=SPEECH_BUDGET_CHARS,
        request_timeout=REQUEST_TIMEOUT,
    )


async def _close_pipeline(pipe: Any) -> None:
    for name in ("stop", "close"):
        method = getattr(pipe, name, None)
        if method is None:
            continue
        try:
            await method()
        except Exception:
            pass


def load_greeting(settings: Settings) -> Optional[bytes]:
    """Cached greeting PCM, or None when absent/unusable."""
    try:
        data = settings.greeting_cache_path().read_bytes()
    except OSError:
        return None
    if not data or len(data) % 2:
        return None
    return data


def store_greeting(settings: Settings, pcm: bytes) -> Path:
    path = settings.greeting_cache_path()
    ensure_dir(settings.state_dir)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(pcm)
    os.replace(temporary, path)
    ensure_private(path)
    return path


async def synthesize_greeting(settings: Settings, *, factory: Any = None) -> bytes:
    pipe = make_pipeline(settings, factory=factory, on_transcript=lambda *_a, **_k: None)
    try:
        pcm = await pipe.synthesize(settings.greeting)
    finally:
        await _close_pipeline(pipe)
    if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2:
        raise CliError("invalid_pcm", "greeting synthesis returned unusable PCM")
    return pcm


async def ensure_greeting(
    settings: Settings, *, log: EventLog, factory: Any = None
) -> bytes:
    """Return greeting PCM, synthesizing and caching it when missing."""
    cached = load_greeting(settings)
    if cached is not None:
        log.event("greeting_cached", path=str(settings.greeting_cache_path()), bytes=len(cached))
        return cached
    pcm = await synthesize_greeting(settings, factory=factory)
    path = store_greeting(settings, pcm)
    log.event("greeting_prepared", path=str(path), bytes=len(pcm))
    return pcm


# ------------------------------------------------------------------------ call #
@dataclass(frozen=True)
class CallStack:
    """Lazily imported media/audio dependencies (kept out of ``--help``/``check``)."""

    PyTgCalls: Any
    filters: Any
    ChatUpdate: Any
    Device: Any
    Direction: Any
    ExternalMedia: Any
    MediaStream: Any
    RecordStream: Any
    AudioParameters: Any
    AudioOutput: Any
    VoicePipeline: Any
    bounded_call: Any
    peer_user: Any


def _import_call_stack() -> CallStack:
    """Import py-tgcalls and local audio modules only when a call actually happens."""
    from pytgcalls import PyTgCalls, filters
    from pytgcalls.types import (
        ChatUpdate,
        Device,
        Direction,
        ExternalMedia,
        MediaStream,
        RecordStream,
    )
    from pytgcalls.types.raw import AudioParameters

    from .audio_output import AudioOutput
    from .call_session import bounded_call

    return CallStack(
        PyTgCalls=PyTgCalls,
        filters=filters,
        ChatUpdate=ChatUpdate,
        Device=Device,
        Direction=Direction,
        ExternalMedia=ExternalMedia,
        MediaStream=MediaStream,
        RecordStream=RecordStream,
        AudioParameters=AudioParameters,
        AudioOutput=AudioOutput,
        VoicePipeline=_voice_pipeline_class(),
        bounded_call=bounded_call,
        peer_user=_peer_user,
    )


async def _wait_for_either(*events: asyncio.Event) -> None:
    if any(event.is_set() for event in events):
        return
    waiters = [asyncio.create_task(event.wait()) for event in events]
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)


async def run_call(
    app: Any,
    settings: Settings,
    policy: CallPolicy,
    *,
    stack: CallStack,
    greeting: bytes,
    log: EventLog,
    stop: Optional[asyncio.Event] = None,
) -> dict:
    """One bounded call: pre-generated greeting, streaming reply loop, guaranteed hangup."""
    owner = await resolve_owner(app, settings, policy, peer_user=stack.peer_user, log=log)
    target = getattr(owner, "id")
    log.event("verified", target=target)

    calls = stack.PyTgCalls(app)
    done = asyncio.Event()
    stop = stop if stop is not None else asyncio.Event()
    utterances: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
    history: list[dict] = []
    tasks: list[asyncio.Task] = []
    metrics = {
        "rx_frames": 0,
        "rx_bytes": 0,
        "tx_frames": 0,
        "transcripts": 0,
        "answers": 0,
        "interruptions": 0,
        "stale_replies": 0,
        "timed_out": 0,
    }
    params = stack.AudioParameters(bitrate=48000, channels=1)

    async def send(frame: bytes) -> None:
        policy.authorize_target(target)
        await calls.send_frame(target, stack.Device.MICROPHONE, frame)
        metrics["tx_frames"] += 1

    output = stack.AudioOutput(send)

    def interrupt(*_args: Any) -> None:
        output.interrupt()
        metrics["interruptions"] += 1

    def transcript(text: str) -> None:
        log.event("user", text=text)
        metrics["transcripts"] += 1
        interrupt()
        try:
            utterances.put_nowait(text)
        except asyncio.QueueFull:
            log.event("error", reason="utterance_queue_full")
            done.set()

    pipe = make_pipeline(
        settings,
        factory=stack.VoicePipeline,
        on_transcript=transcript,
        on_interim=interrupt,
        on_speech_started=interrupt,
    )

    @calls.on_update(
        stack.filters.stream_frame(stack.Direction.INCOMING, stack.Device.MICROPHONE)
    )
    async def incoming(_client: Any, update: Any) -> None:
        if getattr(update, "chat_id", None) != target:
            return  # never transcribe audio from any other chat
        for frame in update.frames:
            pcm = bytes(frame.frame)
            metrics["rx_frames"] += 1
            metrics["rx_bytes"] += len(pcm)
            await pipe.feed_audio(pcm)

    @calls.on_update(
        stack.filters.chat_update(
            stack.ChatUpdate.Status.DISCARDED_CALL | stack.ChatUpdate.Status.BUSY_CALL
        )
    )
    async def remote_status(_client: Any, update: Any) -> None:
        if getattr(update, "chat_id", None) == target:
            log.event("remote_ended", status=str(getattr(update, "status", "")))
            done.set()

    async def keepalive() -> None:
        last_rx = 0
        while not done.is_set() and not stop.is_set():
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            if done.is_set() or stop.is_set():
                return
            if metrics["rx_frames"] == last_rx:
                await pipe.feed_audio(SILENCE_FRAME)
            last_rx = metrics["rx_frames"]
            error = getattr(pipe, "last_error", None)
            if error or bool(getattr(pipe, "streaming_closed", False)):
                log.event("error", reason="asr_error", message=str(error or "asr closed")[:300])
                done.set()
                return

    async def replies() -> None:
        while not done.is_set() and not stop.is_set():
            text = await utterances.get()
            history.append({"role": "user", "content": text})
            generation = output.generation
            started = time.monotonic()
            reply = await pipe.respond(history[-HISTORY_LIMIT:])
            if done.is_set() or stop.is_set():
                return
            if not utterances.empty():
                continue  # newer speech arrived: fold it into the next turn
            if generation != output.generation:
                metrics["stale_replies"] += 1
                continue  # barged in while thinking: never synthesize a stale reply
            pcm = await pipe.synthesize(reply)
            if done.is_set() or stop.is_set() or generation != output.generation:
                metrics["stale_replies"] += 1
                continue  # barged in: drop the stale reply
            history.append({"role": "assistant", "content": reply})
            log.event("assistant", text=reply, latency=round(time.monotonic() - started, 2))
            metrics["answers"] += 1
            await output.play(pcm)
            if any(phrase in text.casefold() for phrase in FAREWELL_PHRASES):
                done.set()
                return

    async def guarded(coro: Any) -> None:
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.event("error", reason=f"{type(exc).__name__}: {str(exc)[:300]}")
            done.set()

    async def conversation() -> None:
        log.event("connected", target=target)
        await pipe.start()  # ASR only after the call is connected
        await calls.record(target, stack.RecordStream(True, params))
        tasks.append(asyncio.create_task(guarded(keepalive())))
        tasks.append(asyncio.create_task(guarded(replies())))
        log.event("assistant", text=settings.greeting, pre_generated=True)
        await output.play(greeting)
        await _wait_for_either(done, stop)

    await calls.start()
    log.event("dialing", target=target, max_seconds=settings.call_max_seconds)
    try:
        await stack.bounded_call(
            calls,
            target,
            stack.MediaStream(stack.ExternalMedia.AUDIO, params),
            conversation,
            seconds=settings.call_max_seconds,
            policy=policy,
        )
    except TimeoutError:
        metrics["timed_out"] = 1
        log.event("hard_timeout", max_seconds=settings.call_max_seconds)
    finally:
        done.set()
        output.interrupt()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await _close_pipeline(pipe)

    if stop.is_set():
        log.event("stopped", reason="signal")
    log.event(
        "ended",
        **metrics,
        stt_dropped_frames=getattr(pipe, "dropped_frames", 0),
        tts_chars=getattr(pipe, "speech_chars_used", 0),
    )
    return metrics


# -------------------------------------------------------------------- commands #
async def cmd_check(settings: Settings, *, policy: CallPolicy, log: EventLog) -> int:
    async with dedicated_client(settings, log=log) as app:
        await run_check(app, settings, policy, log=log)
    return EXIT_OK


async def cmd_login(settings: Settings, *, policy: CallPolicy, log: EventLog) -> int:
    async with dedicated_client(settings, log=log) as app:
        await run_login(app, settings, policy, log=log)
    return EXIT_OK


async def cmd_prepare(settings: Settings, *, log: EventLog) -> int:
    pcm = await ensure_greeting(settings, log=log)
    log.event(
        "prepared",
        path=str(settings.greeting_cache_path()),
        bytes=len(pcm),
        seconds=round(len(pcm) / 2 / 48000, 2),
    )
    return EXIT_OK


async def run_until_stopped(coro: Any, stop: asyncio.Event):
    """Cancel even the ringing phase on SIGTERM, awaiting the hangup finally."""
    work = asyncio.create_task(coro)
    stopped = asyncio.create_task(stop.wait())
    try:
        finished, _ = await asyncio.wait((work, stopped), return_when=asyncio.FIRST_COMPLETED)
        if work in finished:
            return await work
        work.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await work
    finally:
        stopped.cancel()
        if not work.done():
            work.cancel()
        await asyncio.gather(work, stopped, return_exceptions=True)


async def cmd_call(settings: Settings, *, policy: CallPolicy, log: EventLog) -> int:
    async with dedicated_client(settings, log=log) as app:
        if not await app.is_user_authorized():
            raise CliError(
                "not_authorized",
                "the dedicated session is not signed in; run: telegram-live-call login",
            )
        me = await app.get_me()
        policy.authorize_source(getattr(me, "id", None))
        log.event("source_verified", source_id=getattr(me, "id"))
        greeting = await ensure_greeting(settings, log=log)
        stack = _import_call_stack()
        stop = asyncio.Event()
        with install_signal_handlers(stop, log=log):
            await run_until_stopped(
                run_call(app, settings, policy, stack=stack, greeting=greeting, log=log, stop=stop),
                stop,
            )
    return EXIT_OK


# ------------------------------------------------------------------------ main #
def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    command = args.command
    if command == "call" and not getattr(args, "confirm", False):
        EventLog().event(
            "refused",
            reason="confirm_required",
            hint="re-run with --confirm to place exactly one call",
        )
        return EXIT_USAGE

    try:
        settings = load_settings(args.env_file)
    except SettingsError as exc:
        EventLog().event("error", reason="settings", message=str(exc)[:300])
        return EXIT_ERROR

    try:
        log = EventLog(
            secrets=settings_secrets(settings),
            transcript_path=(
                transcript_path(settings) if getattr(args, "save_transcript", False) else None
            ),
        )
    except OSError as exc:
        EventLog().event("error", reason="state_directory", message=str(exc)[:300])
        return EXIT_ERROR
    try:
        policy = build_policy(settings)
        log.event("config", **settings.safe_summary())
        if log.transcript_path is not None:
            log.event("transcript", path=str(log.transcript_path))
        if command == "check":
            return asyncio.run(cmd_check(settings, policy=policy, log=log))
        if command == "login":
            return asyncio.run(cmd_login(settings, policy=policy, log=log))
        if command == "prepare":
            return asyncio.run(cmd_prepare(settings, log=log))
        return asyncio.run(cmd_call(settings, policy=policy, log=log))
    except SessionBusy as exc:
        log.event("error", reason="session_busy", message=str(exc)[:300])
        return EXIT_ERROR
    except CliError as exc:
        log.event("error", reason=exc.reason, message=str(exc)[:300])
        return EXIT_ERROR
    except PermissionError as exc:
        log.event("error", reason="policy", message=str(exc)[:300])
        return EXIT_ERROR
    except SettingsError as exc:
        log.event("error", reason="settings", message=str(exc)[:300])
        return EXIT_ERROR
    except KeyboardInterrupt:
        log.event("stopped", reason="interrupt")
        return EXIT_ERROR
    except Exception as exc:  # pragma: no cover - defensive, never leaks secrets
        log.event("fatal", type=type(exc).__name__, message=str(exc)[:300])
        return EXIT_ERROR
    finally:
        log.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
