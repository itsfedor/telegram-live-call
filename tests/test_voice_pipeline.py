"""Voice pipeline tests; providers are local fakes and credentials synthetic."""

import asyncio
import json
import logging
from contextlib import asynccontextmanager

import pytest
from aiohttp import web

from telegram_live_call import voice_pipeline as vp

DG_KEY = "dg-SECRET-KEY-111"
TTS_KEY = "xi-SECRET-KEY-222"
OR_KEY = "or-SECRET-KEY-333"
VOICE_ID = "example-test-voice"

CTX = "Ты голосовой ассистент. Отвечай кратко по-русски."


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def serve(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


async def noop_transcript(text):  # pragma: no cover - default callback
    pass


def make_pipeline(transcripts=None, on_transcript=None, **over):
    kwargs = dict(
        deepgram_api_key=DG_KEY,
        elevenlabs_api_key=TTS_KEY,
        openrouter_api_key=OR_KEY,
        on_transcript=on_transcript or noop_transcript,
        voice_id=VOICE_ID,
        system_context=CTX,
    )
    kwargs.update(over)
    return vp.VoicePipeline(**kwargs)


def results(transcript, is_final=True, speech_final=True, type_="Results", alternatives=None):
    alts = alternatives if alternatives is not None else [
        {"transcript": transcript, "confidence": 0.98}
    ]
    return {
        "type": type_,
        "is_final": is_final,
        "speech_final": speech_final,
        "channel": {"alternatives": alts},
    }


# --------------------------------------------------------------------------- #
# 1. deepgram request serialization
# --------------------------------------------------------------------------- #
def test_deepgram_params_serialize_required_options():
    p = make_pipeline()
    q = p.deepgram_params()
    assert str(q["model"]) == "nova-3"
    assert str(q["language"]) == "multi"
    assert str(q["encoding"]) == "linear16"
    assert str(q["sample_rate"]) == "48000"
    assert str(q["channels"]) == "1"
    assert str(q["interim_results"]).lower() == "true"
    assert str(q["endpointing"]) == "400"
    # needed for SpeechStarted events (barge-in support)
    assert str(q["vad_events"]).lower() == "true"


def test_defaults_are_overridable_for_provider_tariff_limits():
    assert vp.DEFAULT_CHAT_MODEL == "openai/gpt-4.1-mini"
    p = make_pipeline()
    assert p.chat_model == vp.DEFAULT_CHAT_MODEL
    assert p.tts_model == "eleven_v4"
    assert p.output_format == "pcm_48000"
    q = make_pipeline(tts_model="eleven_flash_v2_5", output_format="pcm_16000")
    assert q.tts_model == "eleven_flash_v2_5" and q.output_format == "pcm_16000"


def test_deepgram_url_carries_params_and_token_authorization():
    async def scenario():
        state = {}

        async def handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            state["query"] = {k: v for k, v in request.query.items()}
            state["auth"] = request.headers.get("Authorization")
            await ws.close()
            return ws

        app = web.Application()
        app.router.add_get("/v1/listen", handler)
        async with serve(app) as base:
            p = make_pipeline(deepgram_url=base.replace("http://", "ws://") + "/v1/listen")
            await p.start()
            await asyncio.sleep(0.05)
            await p.stop()
        return state

    state = asyncio.run(scenario())
    assert state["query"]["model"] == "nova-3"
    assert state["query"]["language"] == "multi"
    assert state["query"]["encoding"] == "linear16"
    assert state["query"]["sample_rate"] == "48000"
    assert state["query"]["channels"] == "1"
    assert state["query"]["interim_results"] == "true"
    assert state["query"]["endpointing"] == "400"
    assert state["auth"] == f"Token {DG_KEY}"


# --------------------------------------------------------------------------- #
# 2. PCM framing
# --------------------------------------------------------------------------- #
def test_pcm_constants_match_telegram_call_format():
    assert vp.SAMPLE_RATE == 48000
    assert vp.PCM_CHANNELS == 1
    assert vp.SAMPLE_WIDTH == 2
    assert vp.FRAME_MS == 20
    assert vp.FRAME_BYTES == 1920  # 20 ms of 48 kHz / 16-bit mono


def test_split_pcm_frames_returns_whole_frames_plus_remainder():
    data = bytes(vp.FRAME_BYTES * 2 + 100)
    frames, rest = vp.split_pcm_frames(data)
    assert [len(f) for f in frames] == [vp.FRAME_BYTES, vp.FRAME_BYTES]
    assert len(rest) == 100


def test_split_pcm_frames_empty_and_short_input():
    frames, rest = vp.split_pcm_frames(b"")
    assert frames == [] and rest == b""
    frames, rest = vp.split_pcm_frames(bytes(vp.FRAME_BYTES - 2))
    assert frames == [] and len(rest) == vp.FRAME_BYTES - 2


@pytest.mark.asyncio
async def test_feed_audio_carries_remainder_across_calls():
    p = make_pipeline()
    assert await p.feed_audio(bytes(1000)) == 0
    assert await p.feed_audio(bytes(1920)) == 1
    assert await p.feed_audio(bytes(920)) == 1
    assert p.pending_frames == 2
    assert p.dropped_frames == 0


@pytest.mark.asyncio
async def test_feed_audio_rejects_odd_byte_counts():
    p = make_pipeline()
    with pytest.raises(ValueError):
        await p.feed_audio(b"\x00\x01\x02")
    assert p.pending_frames == 0
    assert await p.feed_audio(b"") == 0


@pytest.mark.asyncio
async def test_feed_audio_bounded_queue_drops_instead_of_blocking():
    p = make_pipeline(queue_maxsize=1)
    queued = await p.feed_audio(bytes(vp.FRAME_BYTES * 3))
    assert queued == 1
    assert p.pending_frames == 1
    assert p.dropped_frames == 2


# --------------------------------------------------------------------------- #
# 3. endpointing transcript aggregation
# --------------------------------------------------------------------------- #
def test_aggregator_ignores_interims_and_emits_on_speech_final():
    agg = vp.TranscriptAggregator()
    assert agg.feed(results("привет", is_final=False, speech_final=False)) is None
    assert agg.feed(results("как дела", is_final=True, speech_final=False)) is None
    assert agg.feed(results("как дела хорошо", is_final=True, speech_final=True)) == "как дела хорошо"
    # buffer must be empty again
    assert agg.feed(results("", is_final=True, speech_final=False)) is None


def test_aggregator_joins_multiple_final_segments_within_one_utterance():
    agg = vp.TranscriptAggregator()
    assert agg.feed(results("первая часть", is_final=True, speech_final=False)) is None
    assert agg.feed(results("вторая часть", is_final=True, speech_final=False)) is None
    assert agg.feed(results("третья", is_final=True, speech_final=True)) == "первая часть вторая часть третья"


def test_aggregator_handles_cumulative_and_duplicate_final_chunks():
    agg = vp.TranscriptAggregator()
    agg.feed(results("как дела", is_final=True, speech_final=False))
    # cumulative re-send of the same chunk must replace, not duplicate
    agg.feed(results("как дела хорошо", is_final=True, speech_final=False))
    assert agg.feed(results("", is_final=True, speech_final=True)) == "как дела хорошо"

    dup = vp.TranscriptAggregator()
    dup.feed(results("привет", is_final=True, speech_final=False))
    assert dup.feed(results("привет", is_final=True, speech_final=True)) == "привет"


def test_aggregator_handles_noise_metadata_and_empty_transcripts():
    agg = vp.TranscriptAggregator()
    assert agg.feed(results("x", type_="Metadata")) is None
    assert agg.feed(results("", is_final=True, speech_final=False)) is None
    assert agg.feed({"type": "Results"}) is None  # no channel
    assert agg.feed({"type": "Results", "is_final": True, "speech_final": True,
                     "channel": {"alternatives": []}}) is None
    assert agg.feed(results(None, alternatives=[{"transcript": None}])) is None


def test_aggregator_feed_message_parses_text_and_bytes():
    agg = vp.TranscriptAggregator()
    payload = results("готово", is_final=True, speech_final=True)
    assert agg.feed_message(json.dumps(payload)) == "готово"
    assert agg.feed_message(json.dumps(payload).encode()) == "готово"
    assert agg.feed_message("{not json") is None
    assert agg.feed_message(b"\xff\xfe") is None
    assert agg.feed_message(None) is None


def test_aggregator_exposes_latest_interim_for_barge_in():
    agg = vp.TranscriptAggregator()
    assert agg.interim == ""
    agg.feed(results("приве", is_final=False, speech_final=False))
    assert agg.interim == "приве"
    agg.feed(results("привет как", is_final=False, speech_final=False))
    assert agg.interim == "привет как"
    agg.feed(results("привет как дела", is_final=True, speech_final=True))
    assert agg.interim == ""  # cleared when the utterance is finalized


def test_parse_message_returns_dict_or_none():
    assert vp.parse_message('{"type": "SpeechStarted"}') == {"type": "SpeechStarted"}
    assert vp.parse_message(b'{"type": "SpeechStarted"}') == {"type": "SpeechStarted"}
    assert vp.parse_message("[1, 2]") is None
    assert vp.parse_message("nope") is None
    assert vp.parse_message(None) is None


# --------------------------------------------------------------------------- #
# 4. live streaming over a local fake websocket
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_start_streams_pcm_and_emits_only_final_sentences():
    state = {"received": [], "texts": [], "interims": [], "started": 0}
    got = asyncio.Event()

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        state["query"] = dict(request.query)
        state["auth"] = request.headers.get("Authorization")
        async for msg in ws:
            if msg.type == web.WSMsgType.BINARY:
                state["received"].append(msg.data)
                if len(state["received"]) == 2:
                    await ws.send_str(json.dumps({"type": "SpeechStarted", "channel": [0]}))
                    await ws.send_str(json.dumps(results("как дела", is_final=False, speech_final=False)))
                    await ws.send_str(json.dumps(results("как дела хорошо", is_final=True, speech_final=False)))
                    await ws.send_str(json.dumps(results("как дела хорошо", is_final=True, speech_final=True)))
                    await ws.send_str(json.dumps({"type": "UtteranceEnd", "channel": [0]}))
        return ws

    async def on_transcript(text):
        state["texts"].append(text)
        got.set()

    async def on_interim(text):
        state["interims"].append(text)

    def on_speech_started():
        state["started"] += 1

    app = web.Application()
    app.router.add_get("/v1/listen", handler)
    async with serve(app) as base:
        p = make_pipeline(on_transcript=on_transcript,
                          on_interim=on_interim,
                          on_speech_started=on_speech_started,
                          deepgram_url=base.replace("http://", "ws://") + "/v1/listen")
        try:
            await p.start()
            frame_a = b"\x01\x02" * (vp.FRAME_BYTES // 2)
            frame_b = b"\x03\x04" * (vp.FRAME_BYTES // 2)
            await p.feed_audio(frame_a)
            await p.feed_audio(frame_b)
            await asyncio.wait_for(got.wait(), 5)
            await asyncio.sleep(0.1)  # let remaining events drain
        finally:
            await p.stop()

    assert state["texts"] == ["как дела хорошо"]
    assert state["interims"] == ["как дела"]
    assert state["started"] == 1
    assert b"".join(state["received"]) == frame_a + frame_b
    assert state["query"]["language"] == "multi"
    assert state["auth"] == f"Token {DG_KEY}"


# --------------------------------------------------------------------------- #
# 5. ElevenLabs synthesis
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def tts_service(payload=b"\x01\x02" * 240, status=200, body_text="", delay=0.0):
    state = {}

    async def handler(request):
        state["path"] = request.path
        state["query"] = dict(request.query)
        state["key"] = request.headers.get("xi-api-key")
        state["accept"] = request.headers.get("Accept")
        if request.can_read_body:
            state["json"] = await request.json()
        if delay:
            await asyncio.sleep(delay)
        if status != 200:
            return web.Response(status=status, text=body_text)
        return web.Response(body=payload, content_type="application/octet-stream")

    app = web.Application()
    app.router.add_post("/v1/text-to-speech/{voice_id}", handler)
    async with serve(app) as base:
        yield base, state


@pytest.mark.asyncio
async def test_synthesize_sends_expected_request_and_returns_even_pcm():
    async with tts_service() as (base, state):
        p = make_pipeline(tts_base_url=base + "/v1/text-to-speech")
        try:
            pcm = await p.synthesize("Привет, как дела?")
        finally:
            await p.close()
    assert state["path"] == f"/v1/text-to-speech/{VOICE_ID}"
    assert state["query"]["output_format"] == "pcm_48000"
    assert state["key"] == TTS_KEY
    assert state["json"]["text"] == "Привет, как дела?"
    assert state["json"]["model_id"] == "eleven_v4"
    assert isinstance(pcm, bytes) and len(pcm) % 2 == 0 and len(pcm) == 480


@pytest.mark.asyncio
async def test_synthesize_honours_overridden_model_and_output_format():
    async with tts_service() as (base, state):
        p = make_pipeline(tts_base_url=base + "/v1/text-to-speech",
                          tts_model="eleven_flash_v2_5", output_format="pcm_16000",
                          voice_id="someVoiceId123")
        try:
            await p.synthesize("тест")
        finally:
            await p.close()
    assert state["path"] == "/v1/text-to-speech/someVoiceId123"
    assert state["query"]["output_format"] == "pcm_16000"
    assert state["json"]["model_id"] == "eleven_flash_v2_5"


@pytest.mark.asyncio
async def test_synthesize_rejects_odd_byte_count():
    async with tts_service(payload=b"\x01\x02\x03") as (base, _):
        p = make_pipeline(tts_base_url=base + "/v1/text-to-speech")
        try:
            with pytest.raises(vp.VoicePipelineError) as ei:
                await p.synthesize("тест")
        finally:
            await p.close()
    assert "pcm" in str(ei.value).lower()
    assert ei.value.provider == "elevenlabs"


@pytest.mark.asyncio
async def test_synthesize_truncates_to_max_chars_at_sentence_boundary():
    text = "Первое предложение. Второе предложение очень длинное и не влезет целиком."
    async with tts_service() as (base, state):
        p = make_pipeline(tts_base_url=base + "/v1/text-to-speech", max_speech_chars=25)
        try:
            await p.synthesize(text)
        finally:
            await p.close()
    sent = state["json"]["text"]
    assert len(sent) <= 25
    assert sent == "Первое предложение."


def test_limit_speech_chars_keeps_short_text_and_trims_long():
    assert vp.limit_speech_chars("Привет.", 50) == "Привет."
    assert vp.limit_speech_chars("Привет. Как дела? Всё хорошо!", 16) == "Привет."
    assert vp.limit_speech_chars("Привет. Как дела? Всё хорошо!", 24) == "Привет. Как дела?"
    assert vp.limit_speech_chars("без точки очень длинный текст", 6) == "без то"
    assert vp.limit_speech_chars("  Привет.  ", 50) == "Привет."


@pytest.mark.asyncio
async def test_synthesis_budget_aggregates_and_blocks():
    async with tts_service() as (base, state):
        p = make_pipeline(tts_base_url=base + "/v1/text-to-speech", speech_budget_chars=10)
        try:
            await p.synthesize("раз два")
            assert p.speech_chars_used == len("раз два")
            with pytest.raises(vp.VoicePipelineError) as ei:
                await p.synthesize("четыре")
            p.reset_speech_budget()
            assert p.speech_chars_used == 0
            await p.synthesize("ещё")
        finally:
            await p.close()
    assert "budget" in str(ei.value).lower()


# --------------------------------------------------------------------------- #
# 6. OpenRouter respond()
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def or_service(status=200, text="  Привет, я слушаю.  ", body_text="", delay=0.0):
    state = {}

    async def handler(request):
        state["path"] = request.path
        state["auth"] = request.headers.get("Authorization")
        state["json"] = await request.json()
        if delay:
            await asyncio.sleep(delay)
        if status != 200:
            return web.Response(status=status, text=body_text)
        return web.json_response({"choices": [{"message": {"role": "assistant", "content": text}}]})

    app = web.Application()
    app.router.add_post("/api/v1/chat/completions", handler)
    async with serve(app) as base:
        yield base, state


@pytest.mark.asyncio
async def test_respond_sends_system_context_and_model_without_tools():
    async with or_service() as (base, state):
        p = make_pipeline(openrouter_url=base + "/api/v1/chat/completions",
                          chat_model="test/model-x")
        try:
            reply = await p.respond([{"role": "user", "content": "Как дела?"}])
        finally:
            await p.close()
    assert reply == "Привет, я слушаю."
    assert state["json"]["model"] == "test/model-x"
    assert state["json"]["messages"][0] == {"role": "system", "content": CTX}
    assert state["json"]["messages"][1] == {"role": "user", "content": "Как дела?"}
    assert "tools" not in state["json"]
    assert "tool_choice" not in state["json"]
    assert state["auth"] == f"Bearer {OR_KEY}"


@pytest.mark.asyncio
async def test_respond_omits_system_message_when_no_context():
    async with or_service() as (base, state):
        p = make_pipeline(openrouter_url=base + "/api/v1/chat/completions", system_context="")
        try:
            await p.respond([{"role": "user", "content": "да"}])
        finally:
            await p.close()
    assert state["json"]["messages"] == [{"role": "user", "content": "да"}]


# --------------------------------------------------------------------------- #
# 7. sanitized provider errors, bounded timeouts, no key leakage
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_http_error_is_sanitized_and_key_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    leaky = f'{{"error":"invalid api key {TTS_KEY}"}}'
    async with tts_service(status=401, body_text=leaky) as (base, _):
        p = make_pipeline(tts_base_url=base + "/v1/text-to-speech")
        try:
            with pytest.raises(vp.VoicePipelineError) as ei:
                await p.synthesize("тест")
        finally:
            await p.close()
    assert ei.value.status == 401
    assert ei.value.provider == "elevenlabs"
    assert TTS_KEY not in str(ei.value)
    assert TTS_KEY not in repr(ei.value)
    assert "***" in str(ei.value)
    assert TTS_KEY not in caplog.text


@pytest.mark.asyncio
async def test_openrouter_error_is_sanitized():
    async with or_service(status=500, body_text=f"boom {OR_KEY}") as (base, _):
        p = make_pipeline(openrouter_url=base + "/api/v1/chat/completions")
        try:
            with pytest.raises(vp.VoicePipelineError) as ei:
                await p.respond([{"role": "user", "content": "да"}])
        finally:
            await p.close()
    assert ei.value.status == 500
    assert ei.value.provider == "openrouter"
    assert OR_KEY not in str(ei.value)


@pytest.mark.asyncio
async def test_request_timeout_is_bounded_and_sanitized():
    async with or_service(delay=1.0) as (base, _):
        p = make_pipeline(openrouter_url=base + "/api/v1/chat/completions", request_timeout=0.05)
        try:
            with pytest.raises(vp.VoicePipelineError) as ei:
                await p.respond([{"role": "user", "content": "да"}])
        finally:
            await p.close()
    assert "timed out" in str(ei.value).lower()
    assert ei.value.provider == "openrouter"


def test_repr_never_contains_keys():
    p = make_pipeline()
    for key in (DG_KEY, TTS_KEY, OR_KEY):
        assert key not in repr(p)
        assert key not in str(p)


def test_missing_key_fails_fast():
    with pytest.raises(ValueError):
        vp.VoicePipeline(deepgram_api_key="", elevenlabs_api_key=TTS_KEY,
                         openrouter_api_key=OR_KEY, on_transcript=noop_transcript)
    with pytest.raises(ValueError):
        vp.VoicePipeline(deepgram_api_key=DG_KEY, elevenlabs_api_key="",
                         openrouter_api_key=OR_KEY, on_transcript=noop_transcript)
    with pytest.raises(ValueError):
        vp.VoicePipeline(deepgram_api_key=DG_KEY, elevenlabs_api_key=TTS_KEY,
                         openrouter_api_key="", on_transcript=noop_transcript)
    with pytest.raises(ValueError):
        make_pipeline(max_speech_chars=0)


@pytest.mark.asyncio
async def test_close_is_idempotent_and_safe_before_start():
    p = make_pipeline()
    await p.close()
    await p.stop()
    await p.close()
