"""Settings: explicit dotenv file + os.environ, strict IDs, secret-safe errors."""
import json
from pathlib import Path

import pytest

from conftest import SANITIZED_ENV, SECRET_VALUES, write_dotenv


def _from_env(env_file=None, environ=None):
    from telegram_live_call.settings import from_env

    if environ is None:
        return from_env(env_file)
    return from_env(env_file, environ=environ)


def test_from_env_reads_explicit_dotenv_file_and_defaults(env_file):
    settings = _from_env(env_file)

    assert settings.telegram_api_id == 123456
    assert settings.call_owner_id == 100200300
    assert settings.call_source_id == 400500600
    assert settings.telegram_api_hash == SANITIZED_ENV["TELEGRAM_API_HASH"]
    assert settings.chat_model == "openai/gpt-4.1-mini"
    assert settings.tts_model == "eleven_v4"
    assert settings.call_max_seconds == 60
    assert settings.context_file is None
    assert settings.call_owner_username is None


def test_from_env_reads_os_environ_when_no_file(monkeypatch):
    for key, value in SANITIZED_ENV.items():
        monkeypatch.setenv(key, value)

    settings = _from_env()

    assert settings.call_owner_id == 100200300


def test_explicit_env_file_must_exist(tmp_path):
    from telegram_live_call.settings import SettingsError

    with pytest.raises(SettingsError) as excinfo:
        _from_env(tmp_path / "missing.env")

    assert "env-file" in str(excinfo.value).lower()


def test_missing_required_settings_name_keys_only(tmp_path):
    from telegram_live_call.settings import SettingsError

    values = dict(SANITIZED_ENV)
    values.pop("DEEPGRAM_API_KEY")
    values.pop("ELEVENLABS_VOICE_ID")
    path = write_dotenv(tmp_path / "call.env", values)

    with pytest.raises(SettingsError) as excinfo:
        _from_env(path, environ={})

    message = str(excinfo.value)
    assert "DEEPGRAM_API_KEY" in message
    assert "ELEVENLABS_VOICE_ID" in message
    for secret in SECRET_VALUES:
        assert secret not in message


def test_blank_value_counts_as_missing(tmp_path):
    from telegram_live_call.settings import SettingsError

    path = write_dotenv(tmp_path / "call.env", {**SANITIZED_ENV, "OPENROUTER_API_KEY": ""})

    with pytest.raises(SettingsError) as excinfo:
        _from_env(path, environ={})

    assert "OPENROUTER_API_KEY" in str(excinfo.value)


@pytest.mark.parametrize("bad", ["0", "-5", "abc", "1.5", "1_2", "12abc", "١٢٣", " "])
def test_rejects_non_canonical_or_non_positive_ids(make_settings, bad):
    from telegram_live_call.settings import SettingsError

    with pytest.raises(SettingsError):
        make_settings(CALL_OWNER_ID=bad)


@pytest.mark.parametrize("bad", ["0", "-1", "not-a-number"])
def test_rejects_bad_source_id(make_settings, bad):
    from telegram_live_call.settings import SettingsError

    with pytest.raises(SettingsError):
        make_settings(CALL_SOURCE_ID=bad)


def test_accepts_whitespace_padded_ids(make_settings):
    settings = make_settings(CALL_OWNER_ID=" 100200300 ")

    assert settings.call_owner_id == 100200300
    assert type(settings.call_owner_id) is int


def test_rejects_identical_owner_and_source(make_settings):
    from telegram_live_call.settings import SettingsError

    with pytest.raises(SettingsError):
        make_settings(CALL_SOURCE_ID="100200300")


@pytest.mark.parametrize("value,expected", [("1", 1), ("300", 300), ("60", 60)])
def test_call_max_seconds_in_range(make_settings, value, expected):
    assert make_settings(CALL_MAX_SECONDS=value).call_max_seconds == expected


@pytest.mark.parametrize("bad", ["0", "301", "600", "abc", "1.5", "-1", ""])
def test_call_max_seconds_out_of_range(make_settings, bad):
    from telegram_live_call.settings import SettingsError

    with pytest.raises(SettingsError) as excinfo:
        make_settings(CALL_MAX_SECONDS=bad)

    assert "CALL_MAX_SECONDS" in str(excinfo.value)


def test_env_file_wins_and_environ_fills_gaps(tmp_path, fake_home):
    file_values = dict(SANITIZED_ENV)
    file_values.pop("DEEPGRAM_API_KEY")
    path = write_dotenv(
        tmp_path / "call.env", {**file_values, "CHAT_MODEL": "file/model"}
    )
    environ = {
        "CHAT_MODEL": "ambient/model",
        "TTS_MODEL": "ambient_tts",
        "DEEPGRAM_API_KEY": "dg-from-environ",
    }

    settings = _from_env(path, environ)

    assert settings.chat_model == "file/model"
    assert settings.tts_model == "ambient_tts"
    assert settings.deepgram_api_key == "dg-from-environ"


def test_repr_and_summary_never_expose_secrets(env_file):
    settings = _from_env(env_file)

    for text in (repr(settings), str(settings), json.dumps(settings.safe_summary())):
        for secret in SECRET_VALUES:
            assert secret not in text

    summary = settings.safe_summary()
    assert summary["owner_id"] == 100200300
    assert summary["source_id"] == 400500600
    assert summary["max_seconds"] == 60
    assert summary["chat_model"] == "openai/gpt-4.1-mini"
    assert summary["context"] == "neutral"
    assert summary["voice_configured"] is True
    assert set(summary) == {
        "owner_id",
        "source_id",
        "owner_username_set",
        "chat_model",
        "tts_model",
        "max_seconds",
        "context",
        "context_file",
        "session_path",
        "state_dir",
        "voice_configured",
        "providers",
    }


def test_neutral_system_prompt_when_no_context_file(env_file):
    from telegram_live_call.settings import NEUTRAL_SYSTEM_PROMPT

    settings = _from_env(env_file)
    prompt = settings.system_context()

    assert prompt == NEUTRAL_SYSTEM_PROMPT
    assert 20 <= len(prompt) <= 200
    assert not any(ch.isdigit() for ch in prompt)
    assert "@" not in prompt and "\n" not in prompt
    assert prompt.count(".") <= 2
    assert any("а" <= ch.lower() <= "я" for ch in prompt)


def test_context_file_is_used_when_configured(tmp_path, fake_home):
    context = tmp_path / "context.txt"
    context.write_text("  Короткая заметка о разговоре.  ", encoding="utf-8")
    path = write_dotenv(
        tmp_path / "call.env", {**SANITIZED_ENV, "CONTEXT_FILE": str(context)}
    )

    settings = _from_env(path, environ={})

    assert settings.context_file == context
    assert settings.system_context() == "Короткая заметка о разговоре."
    assert settings.safe_summary()["context"] == "file"


def test_missing_context_file_fails_closed(tmp_path, fake_home):
    from telegram_live_call.settings import SettingsError

    path = write_dotenv(
        tmp_path / "call.env",
        {**SANITIZED_ENV, "CONTEXT_FILE": str(tmp_path / "nope.txt")},
    )

    with pytest.raises(SettingsError) as excinfo:
        _from_env(path, environ={})

    assert "CONTEXT_FILE" in str(excinfo.value)


def test_empty_context_file_fails_closed(tmp_path, fake_home):
    from telegram_live_call.settings import SettingsError

    empty = tmp_path / "empty.txt"
    empty.write_text("   \n", encoding="utf-8")
    path = write_dotenv(tmp_path / "call.env", {**SANITIZED_ENV, "CONTEXT_FILE": str(empty)})

    with pytest.raises(SettingsError):
        _from_env(path, environ={})


def test_default_paths_live_under_home_and_not_hardcoded(env_file, fake_home):
    settings = _from_env(env_file)
    state = fake_home / ".local" / "state" / "telegram-live-call"

    assert settings.state_dir == state
    assert settings.session_path == state / "telegram"
    assert settings.greeting_cache_path().parent == state
    assert settings.session_path.relative_to(fake_home)


def test_state_dir_and_session_path_overrides(tmp_path, fake_home):
    state = tmp_path / "private-state"
    session = tmp_path / "sessions" / "dedicated"
    path = write_dotenv(
        tmp_path / "call.env",
        {**SANITIZED_ENV, "STATE_DIR": str(state), "SESSION_PATH": str(session)},
    )

    settings = _from_env(path, environ={})

    assert settings.state_dir == state
    assert settings.session_path == session
    assert settings.greeting_cache_path().parent == state


def test_greeting_cache_path_is_stable_and_voice_specific(make_settings, tmp_path):
    first = make_settings()
    second = make_settings()
    other_voice = make_settings(ELEVENLABS_VOICE_ID="another-sanitized-voice")

    assert first.greeting_cache_path() == second.greeting_cache_path()
    assert first.greeting_cache_path() != other_voice.greeting_cache_path()
    assert first.greeting_cache_path().suffix == ".pcm"
    assert SECRET_VALUES[4] not in first.greeting_cache_path().name


def test_greeting_default_is_neutral_and_overridable(make_settings):
    default = make_settings()
    custom = make_settings(CALL_GREETING="Привет, это проверка связи.")

    assert default.greeting
    assert custom.greeting == "Привет, это проверка связи."
    assert not any(ch.isdigit() for ch in default.greeting)
    assert "@" not in default.greeting


def test_owner_username_is_optional_and_normalized(make_settings):
    from telegram_live_call.settings import SettingsError

    assert make_settings(CALL_OWNER_USERNAME="@some_owner").call_owner_username == "some_owner"
    with pytest.raises(SettingsError):
        make_settings(CALL_OWNER_USERNAME="bad name")


def test_direct_construction_validates_paths(tmp_path):
    from telegram_live_call.settings import Settings, SettingsError

    with pytest.raises(SettingsError):
        Settings(
            telegram_api_id=1,
            telegram_api_hash="",
            call_owner_id=1,
            call_source_id=2,
            deepgram_api_key="k",
            elevenlabs_api_key="k",
            elevenlabs_voice_id="v",
            openrouter_api_key="k",
            session_path=tmp_path / "s",
            state_dir=tmp_path / "state",
        )
