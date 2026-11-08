import pytest
from telegram_live_call.voice_pipeline import VoicePipeline


def test_no_personal_voice_default():
    with pytest.raises(ValueError,match='voice_id'):
        VoicePipeline(deepgram_api_key='test-dg',elevenlabs_api_key='test-tts',openrouter_api_key='test-llm',on_transcript=lambda t:None)
