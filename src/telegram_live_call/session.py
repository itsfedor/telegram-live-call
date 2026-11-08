"""Dedicated Telethon session: private paths, single-process lock, client factory.

The CLI never shares the assistant account with another client. A dedicated
SQLite session file lives under the private state directory (``SESSION_PATH``,
default ``~/.local/state/telegram-live-call/telegram``); Telethon appends the
``.session`` suffix itself, so the configured path is passed through unchanged
and :func:`session_file_path` computes the real file for permissions.

Concurrency is excluded with a non-blocking ``fcntl`` advisory lock that is
acquired *before* any ``connect()``/login. That is why this package is Linux and
macOS only — on other platforms :class:`SessionLock` fails loudly instead of
running unlocked.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional, Union

from .settings import Settings

try:  # POSIX only; absence is a hard, explicit error rather than silent skipping.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX interpreter
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "SessionBusy",
    "SessionLock",
    "build_client",
    "ensure_dir",
    "ensure_private",
    "lock_path",
    "session_file_path",
]

PathLike = Union[str, "os.PathLike[str]"]
_PLATFORM_MESSAGE = "A dedicated session lock requires fcntl (Linux or macOS only)"


class SessionBusy(RuntimeError):
    """Another process holds the dedicated session; nothing was connected."""


def session_file_path(session_path: PathLike) -> Path:
    """Actual SQLite session file Telethon uses (adds ``.session`` when missing)."""
    path = Path(session_path).expanduser()
    return path if path.suffix == ".session" else path.with_name(path.name + ".session")


def lock_path(session_path: PathLike) -> Path:
    """Advisory lock file guarding the dedicated session."""
    return session_file_path(session_path).with_name(session_file_path(session_path).name + ".lock")


def ensure_dir(path: PathLike) -> Path:
    """Create ``path`` (and parents) readable only by the owner."""
    directory = Path(path).expanduser()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    return directory


def ensure_private(path: PathLike) -> bool:
    """Best-effort ``chmod 600`` for a session/transcript/log file."""
    target = Path(path).expanduser()
    try:
        os.chmod(target, 0o600)
    except OSError:
        return False
    return True


def _open_lock_file(path: Path) -> int:
    ensure_dir(path.parent)
    return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)


class SessionLock:
    """Non-blocking exclusive lock over one dedicated session path."""

    def __init__(self, session_path: PathLike) -> None:
        self.session_path = Path(session_path).expanduser()
        self.path = lock_path(self.session_path)
        self._fd: Optional[int] = None

    @property
    def locked(self) -> bool:
        return self._fd is not None

    def acquire(self) -> "SessionLock":
        if fcntl is None:
            raise RuntimeError(_PLATFORM_MESSAGE)
        if self._fd is not None:
            return self
        fd = _open_lock_file(self.path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise SessionBusy(
                "another telegram-live-call process holds this dedicated session "
                f"(lock: {self.path})"
            ) from None
        self._fd = fd
        return self

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self) -> "SessionLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def build_client(settings: Settings) -> Any:
    """Create the dedicated Telethon client. Never touches an existing personal session."""
    try:
        from telethon import TelegramClient
    except ImportError:  # pragma: no cover - dependency always installed with the package
        raise RuntimeError("Telethon is required: install telegram-live-call with dependencies") from None

    ensure_dir(settings.state_dir)
    ensure_dir(settings.session_path.parent)
    return TelegramClient(
        str(settings.session_path),
        settings.telegram_api_id,
        settings.telegram_api_hash,
        use_ipv6=False,
    )


def next_state_file(directory: PathLike, prefix: str, suffix: str = ".jsonl") -> Path:
    """Timestamped file name inside the private state directory (no TMPDIR reliance)."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = Path(directory).expanduser() / f"{prefix}-{stamp}{suffix}"
    candidate, index = base, 0
    while candidate.exists():
        index += 1
        candidate = base.with_name(f"{base.stem}-{index}{base.suffix}")
    return candidate
