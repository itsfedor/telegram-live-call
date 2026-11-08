import asyncio
from contextlib import asynccontextmanager
import pytest


def test_default_prompt_cannot_claim_task_tool_access():
    from telegram_live_call.settings import NEUTRAL_SYSTEM_PROMPT
    assert 'нет доступа' in NEUTRAL_SYSTEM_PROMPT.casefold()


@pytest.mark.asyncio
async def test_stop_cancels_ringing_and_runs_cleanup():
    from telegram_live_call.cli import run_until_stopped
    stop=asyncio.Event();started=asyncio.Event();cleaned=[]
    async def ringing():
        started.set()
        try:await asyncio.sleep(30)
        finally:cleaned.append(True)
    task=asyncio.create_task(run_until_stopped(ringing(),stop))
    await started.wait();stop.set()
    await asyncio.wait_for(task,timeout=0.2)
    assert cleaned==[True]


@pytest.mark.asyncio
async def test_clean_asr_disconnect_is_observable():
    from aiohttp import web
    from telegram_live_call.voice_pipeline import VoicePipeline
    async def handler(request):
        ws=web.WebSocketResponse()
        await ws.prepare(request)
        await ws.close()
        return ws
    app=web.Application();app.router.add_get('/listen',handler)
    runner=web.AppRunner(app);await runner.setup()
    site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    port=runner.addresses[0][1]
    p=VoicePipeline(deepgram_api_key='test-dg',elevenlabs_api_key='test-el',openrouter_api_key='test-or',voice_id='test-voice',on_transcript=lambda _:None,deepgram_url=f'http://127.0.0.1:{port}/listen')
    try:
        await p.start()
        await asyncio.sleep(0.05)
        assert p.streaming_closed is True
    finally:
        await p.stop();await runner.cleanup()
