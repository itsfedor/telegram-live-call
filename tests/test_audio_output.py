import pytest

@pytest.mark.asyncio
async def test_pcm_is_padded_to_ten_millisecond_frames():
    from telegram_live_call.audio_output import AudioOutput
    sent=[]
    async def send(frame):sent.append(frame)
    output=AudioOutput(send)
    await output.play(b'\x01\x00'*500)
    assert [len(x) for x in sent] == [960,960]
    assert sent[1][40:] == b'\0'*920

@pytest.mark.asyncio
async def test_interrupt_drops_rest_of_current_audio():
    from telegram_live_call.audio_output import AudioOutput
    sent=[]
    async def send(frame):
        sent.append(frame)
        output.interrupt()
    output=AudioOutput(send)
    await output.play(b'\0'*9600)
    assert len(sent)==1

@pytest.mark.asyncio
async def test_invalid_pcm_rejected():
    from telegram_live_call.audio_output import AudioOutput
    async def send(frame):raise AssertionError()
    output=AudioOutput(send)
    with pytest.raises(ValueError):await output.play(b'\1')
