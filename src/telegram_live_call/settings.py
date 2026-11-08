"""Configuration for the public Telegram live-call CLI.

Everything is read from an explicit dotenv file and/or ``os.environ`` — there is
no bundled default, no personal identifier and no fallback account. Values in
the explicit ``--env-file`` take precedence over ambient environment variables;
ambient variables only fill gaps.

Secrets (API hash, provider keys, voice id) never appear in ``repr``, in error
messages or in ``safe_summary()``; errors name the offending key only.

The optional ``CONTEXT_FILE`` body is sent to the external reply model, and the
spoken conversation is sent to the STT/TTS/reply providers named in
``PROVIDER_SERVICES``. Point ``CONTEXT_FILE`` at a short, non-sensitive note.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from dotenv import dotenv_values

__all__ = [
    "DEFAULT_CALL_MAX_SECONDS",
    "DEFAULT_CHAT_MODEL",
    "DEFAULT_GREETING",
    "DEFAULT_TTS_MODEL",
    "MAX_CALL_SECONDS",
    "MIN_CALL_SECONDS",
    "NEUTRAL_SYSTEM_PROMPT",
    "PROVIDER_SERVICES",
    "REQUIRED_KEYS",
    "Settings",
    "SettingsError",
    "default_session_path",
    "default_state_dir",
    "from_env",
]

REQUIRED_KEYS = (
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "CALL_OWNER_ID",
    "CALL_SOURCE_ID",
    "DEEPGRAM_API_KEY",
    "ELEVENLABS_API_KEY",
    "ELEVENLABS_VOICE_ID",
    "OPENROUTER_API_KEY",
)
OPTIONAL_KEYS = (
    "CHAT_MODEL",
    "TTS_MODEL",
    "CALL_MAX_SECONDS",
    "CALL_GREETING",
    "CALL_OWNER_USERNAME",
    "CONTEXT_FILE",
    "SESSION_PATH",
    "STATE_DIR",
)

DEFAULT_CHAT_MODEL = "openai/gpt-4.1-mini"
DEFAULT_TTS_MODEL = "eleven_v4"
DEFAULT_CALL_MAX_SECONDS = 60
MIN_CALL_SECONDS = 1
MAX_CALL_SECONDS = 300

#: Neutral, second-person-free prompt used when no CONTEXT_FILE is configured.
NEUTRAL_SYSTEM_PROMPT = (
    "Ты голосовой ИИ-ассистент; говори кратко по-русски, задавай один вопрос. "
    "У тебя нет доступа к инструментам, Notion и календарю: только обсуждай решения, "
    "не утверждай, что что-то прочитал или изменил."
)
DEFAULT_GREETING = "Здравствуйте. Это голосовой ассистент, проверка связи. Вы меня слышите?"

#: Third parties that receive audio/text while a call runs (documented, opt-in by configuring keys).
PROVIDER_SERVICES = ("deepgram", "elevenlabs", "openrouter")

_INT_RE = re.compile(r"\A[0-9]+\Z")
_USERNAME_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9_]{3,31}\Z")


class SettingsError(ValueError):
    """Invalid or missing configuration. Messages name keys, never values."""


def default_state_dir() -> Path:
    """Private state directory for sessions, greeting cache and transcripts."""
    return Path.home() / ".local" / "state" / "telegram-live-call"


def default_session_path() -> Path:
    """Dedicated Telethon session path (Telethon appends ``.session``)."""
    return default_state_dir() / "telegram"


def _positive_id(name: str, raw: str) -> int:
    text = (raw or "").strip()
    if not _INT_RE.match(text) or int(text) <= 0:
        raise SettingsError(f"{name} must be a positive integer Telegram ID")
    return int(text)


def _call_seconds(raw: str) -> int:
    text = (raw or "").strip()
    if not _INT_RE.match(text):
        raise SettingsError(
            f"CALL_MAX_SECONDS must be an integer in {MIN_CALL_SECONDS}..{MAX_CALL_SECONDS}"
        )
    value = int(text)
    if not MIN_CALL_SECONDS <= value <= MAX_CALL_SECONDS:
        raise SettingsError(
            f"CALL_MAX_SECONDS must be an integer in {MIN_CALL_SECONDS}..{MAX_CALL_SECONDS}"
        )
    return value


def _required_text(name: str, raw: Optional[str]) -> str:
    text = (raw or "").strip()
    if not text:
        raise SettingsError(f"{name} must be a non-empty string")
    return text


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated, secret-safe configuration for one dedicated assistant account."""

    telegram_api_id: int
    telegram_api_hash: str = field(repr=False)
    call_owner_id: int
    call_source_id: int
    deepgram_api_key: str = field(repr=False)
    elevenlabs_api_key: str = field(repr=False)
    elevenlabs_voice_id: str = field(repr=False)
    openrouter_api_key: str = field(repr=False)
    chat_model: str = DEFAULT_CHAT_MODEL
    tts_model: str = DEFAULT_TTS_MODEL
    call_max_seconds: int = DEFAULT_CALL_MAX_SECONDS
    greeting: str = field(default=DEFAULT_GREETING, repr=False)
    context_file: Optional[Path] = None
    session_path: Path = field(default_factory=default_session_path)
    state_dir: Path = field(default_factory=default_state_dir)
    call_owner_username: Optional[str] = None

    def __post_init__(self) -> None:
        for name, value in (("CALL_OWNER_ID", self.call_owner_id), ("CALL_SOURCE_ID", self.call_source_id)):
            if type(value) is not int or value <= 0:
                raise SettingsError(f"{name} must be a positive integer Telegram ID")
        if self.call_owner_id == self.call_source_id:
            raise SettingsError("CALL_SOURCE_ID must differ from CALL_OWNER_ID")
        if type(self.telegram_api_id) is not int or self.telegram_api_id <= 0:
            raise SettingsError("TELEGRAM_API_ID must be a positive integer")
        for name, value in (
            ("TELEGRAM_API_HASH", self.telegram_api_hash),
            ("DEEPGRAM_API_KEY", self.deepgram_api_key),
            ("ELEVENLABS_API_KEY", self.elevenlabs_api_key),
            ("ELEVENLABS_VOICE_ID", self.elevenlabs_voice_id),
            ("OPENROUTER_API_KEY", self.openrouter_api_key),
            ("CHAT_MODEL", self.chat_model),
            ("TTS_MODEL", self.tts_model),
            ("CALL_GREETING", self.greeting),
        ):
            if not isinstance(value, str) or not value.strip():
                raise SettingsError(f"{name} must be a non-empty string")
        if type(self.call_max_seconds) is not int or not (
            MIN_CALL_SECONDS <= self.call_max_seconds <= MAX_CALL_SECONDS
        ):
            raise SettingsError(
                f"CALL_MAX_SECONDS must be an integer in {MIN_CALL_SECONDS}..{MAX_CALL_SECONDS}"
            )
        object.__setattr__(self, "session_path", Path(self.session_path).expanduser())
        object.__setattr__(self, "state_dir", Path(self.state_dir).expanduser())
        if self.context_file is not None:
            context = Path(self.context_file).expanduser()
            object.__setattr__(self, "context_file", context)
            if not context.is_file():
                raise SettingsError("CONTEXT_FILE must point at a readable file")
            try:
                body = context.read_text(encoding="utf-8").strip()
            except OSError:
                raise SettingsError("CONTEXT_FILE must point at a readable file") from None
            if not body:
                raise SettingsError("CONTEXT_FILE must not be empty")
        if self.call_owner_username is not None and not _USERNAME_RE.match(
            self.call_owner_username
        ):
            raise SettingsError("CALL_OWNER_USERNAME must be a plain Telegram username")

    # ---------------------------------------------------------------- derived #
    def system_context(self) -> str:
        """System prompt for the reply model: CONTEXT_FILE body or a neutral default."""
        if self.context_file is None:
            return NEUTRAL_SYSTEM_PROMPT
        try:
            text = self.context_file.read_text(encoding="utf-8").strip()
        except OSError:
            raise SettingsError("CONTEXT_FILE must point at a readable file") from None
        if not text:
            raise SettingsError("CONTEXT_FILE must not be empty")
        return text

    def greeting_cache_path(self) -> Path:
        """Greeting PCM cache path, keyed by text + voice + TTS model (no secrets in the name)."""
        key = "\x00".join((self.greeting, self.elevenlabs_voice_id, self.tts_model))
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        return self.state_dir / f"greeting-{digest}.pcm"

    def safe_summary(self) -> dict:
        """Loggable view: identifiers and options only, never credentials."""
        return {
            "owner_id": self.call_owner_id,
            "source_id": self.call_source_id,
            "owner_username_set": self.call_owner_username is not None,
            "chat_model": self.chat_model,
            "tts_model": self.tts_model,
            "max_seconds": self.call_max_seconds,
            "context": "file" if self.context_file else "neutral",
            "context_file": str(self.context_file) if self.context_file else None,
            "session_path": str(self.session_path),
            "state_dir": str(self.state_dir),
            "voice_configured": bool(self.elevenlabs_voice_id),
            "providers": list(PROVIDER_SERVICES),
        }


def _collect(env_file: Optional[Path], environ: Mapping[str, str]) -> dict[str, str]:
    """Merge an explicit dotenv file (wins) over ambient environment values."""
    present: dict[str, str] = {}
    if env_file is not None:
        path = Path(env_file).expanduser()
        if not path.is_file():
            raise SettingsError(f"--env-file not found: {path}")
        try:
            values = dotenv_values(str(path))
        except (OSError, ValueError):
            raise SettingsError(f"--env-file could not be parsed: {path}") from None
        for key, value in values.items():
            if isinstance(value, str):
                present[key] = value
    for key in REQUIRED_KEYS + OPTIONAL_KEYS:
        if key not in present:
            raw = environ.get(key)
            if isinstance(raw, str):
                present[key] = raw
    return present


def _optional(present: Mapping[str, str], key: str) -> Optional[str]:
    if key not in present:
        return None
    text = (present[key] or "").strip()
    if not text:
        raise SettingsError(f"{key} must not be empty when set")
    return text


def from_env(
    env_file: Optional[Path] = None, environ: Optional[Mapping[str, str]] = None
) -> Settings:
    """Build :class:`Settings` from an explicit dotenv file plus ``os.environ``."""
    present = _collect(env_file, os.environ if environ is None else environ)

    missing = [key for key in REQUIRED_KEYS if not (present.get(key) or "").strip()]
    if missing:
        raise SettingsError("missing required setting(s): " + ", ".join(missing))

    state_value = _optional(present, "STATE_DIR")
    state_dir = Path(state_value).expanduser() if state_value else default_state_dir()
    session_value = _optional(present, "SESSION_PATH")
    session_path = Path(session_value).expanduser() if session_value else state_dir / "telegram"
    context_value = _optional(present, "CONTEXT_FILE")
    username = _optional(present, "CALL_OWNER_USERNAME")
    if username is not None:
        username = username.lstrip("@")
    seconds_value = _optional(present, "CALL_MAX_SECONDS")

    return Settings(
        telegram_api_id=_positive_id("TELEGRAM_API_ID", present["TELEGRAM_API_ID"]),
        telegram_api_hash=_required_text("TELEGRAM_API_HASH", present["TELEGRAM_API_HASH"]),
        call_owner_id=_positive_id("CALL_OWNER_ID", present["CALL_OWNER_ID"]),
        call_source_id=_positive_id("CALL_SOURCE_ID", present["CALL_SOURCE_ID"]),
        deepgram_api_key=_required_text("DEEPGRAM_API_KEY", present["DEEPGRAM_API_KEY"]),
        elevenlabs_api_key=_required_text("ELEVENLABS_API_KEY", present["ELEVENLABS_API_KEY"]),
        elevenlabs_voice_id=_required_text("ELEVENLABS_VOICE_ID", present["ELEVENLABS_VOICE_ID"]),
        openrouter_api_key=_required_text("OPENROUTER_API_KEY", present["OPENROUTER_API_KEY"]),
        chat_model=_optional(present, "CHAT_MODEL") or DEFAULT_CHAT_MODEL,
        tts_model=_optional(present, "TTS_MODEL") or DEFAULT_TTS_MODEL,
        call_max_seconds=(
            _call_seconds(seconds_value) if seconds_value is not None else DEFAULT_CALL_MAX_SECONDS
        ),
        greeting=_optional(present, "CALL_GREETING") or DEFAULT_GREETING,
        context_file=Path(context_value).expanduser() if context_value else None,
        session_path=session_path,
        state_dir=state_dir,
        call_owner_username=username,
    )
