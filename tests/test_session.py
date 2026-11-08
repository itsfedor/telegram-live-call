"""Dedicated session: private paths, fcntl single-process lock, client factory."""
import os
import stat
import sys
import types
from pathlib import Path

import pytest


def mode_of(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_session_file_path_appends_suffix_once(tmp_path):
    from telegram_live_call.session import session_file_path

    assert session_file_path(tmp_path / "telegram") == tmp_path / "telegram.session"
    assert session_file_path(tmp_path / "custom.session") == tmp_path / "custom.session"
    assert session_file_path(str(tmp_path / "as-string")) == tmp_path / "as-string.session"


def test_lock_path_sits_next_to_session_file(tmp_path):
    from telegram_live_call.session import lock_path

    assert lock_path(tmp_path / "telegram") == tmp_path / "telegram.session.lock"


def test_second_lock_fails_closed(tmp_path):
    from telegram_live_call.session import SessionBusy, SessionLock

    first = SessionLock(tmp_path / "telegram")
    second = SessionLock(tmp_path / "telegram")
    first.acquire()
    try:
        with pytest.raises(SessionBusy) as excinfo:
            second.acquire()
        assert "session" in str(excinfo.value).lower()
        assert second.locked is False
    finally:
        first.release()

    assert first.locked is False


def test_lock_file_is_private_and_directory_is_private(tmp_path):
    from telegram_live_call.session import SessionLock, lock_path

    session_path = tmp_path / "private" / "telegram"
    lock = SessionLock(session_path)
    lock.acquire()
    try:
        assert mode_of(lock_path(session_path)) == 0o600
        assert mode_of(session_path.parent) == 0o700
    finally:
        lock.release()


def test_lock_released_allows_reacquire(tmp_path):
    from telegram_live_call.session import SessionLock

    first = SessionLock(tmp_path / "telegram")
    first.acquire()
    first.release()
    second = SessionLock(tmp_path / "telegram")
    second.acquire()
    try:
        assert second.locked is True
    finally:
        second.release()


def test_lock_released_when_body_raises(tmp_path):
    from telegram_live_call.session import SessionLock

    with pytest.raises(RuntimeError):
        with SessionLock(tmp_path / "telegram") as lock:
            assert lock.locked is True
            raise RuntimeError("boom")

    again = SessionLock(tmp_path / "telegram")
    again.acquire()
    assert again.locked is True
    again.release()


def test_double_release_is_harmless(tmp_path):
    from telegram_live_call.session import SessionLock

    lock = SessionLock(tmp_path / "telegram")
    lock.acquire()
    lock.release()
    lock.release()
    assert lock.locked is False


def test_ensure_private_chmods_existing_file(tmp_path):
    from telegram_live_call.session import ensure_private

    target = tmp_path / "telegram.session"
    target.write_bytes(b"sqlite")
    os.chmod(target, 0o644)

    assert ensure_private(target) is True
    assert mode_of(target) == 0o600
    assert ensure_private(tmp_path / "missing.session") is False


def test_ensure_private_survives_chmod_failure(tmp_path, monkeypatch):
    from telegram_live_call.session import ensure_private

    target = tmp_path / "telegram.session"
    target.write_bytes(b"x")

    def boom(*args, **kwargs):
        raise OSError("nope")

    monkeypatch.setattr(os, "chmod", boom)
    assert ensure_private(target) is False


def test_lock_requires_fcntl(tmp_path, monkeypatch):
    import telegram_live_call.session as session

    monkeypatch.setattr(session, "fcntl", None)
    with pytest.raises(RuntimeError) as excinfo:
        session.SessionLock(tmp_path / "telegram").acquire()
    assert "Linux" in str(excinfo.value) or "macOS" in str(excinfo.value)


def test_build_client_uses_dedicated_session_path(make_settings, monkeypatch):
    import telegram_live_call.session as session

    settings = make_settings()
    captured = {}

    class FakeClient:
        def __init__(self, path, api_id, api_hash, **kwargs):
            captured.update(path=path, api_id=api_id, api_hash=api_hash, kwargs=kwargs)

    fake_module = types.ModuleType("telethon")
    fake_module.TelegramClient = FakeClient
    monkeypatch.setitem(sys.modules, "telethon", fake_module)

    client = session.build_client(settings)

    assert isinstance(client, FakeClient)
    assert captured["path"] == str(settings.session_path)
    assert not captured["path"].endswith(".session")
    assert captured["api_id"] == settings.telegram_api_id
    assert captured["api_hash"] == settings.telegram_api_hash
    assert captured["kwargs"].get("use_ipv6") is False
    assert mode_of(settings.session_path.parent) == 0o700


def test_build_client_reports_missing_telethon(make_settings, monkeypatch):
    import telegram_live_call.session as session

    monkeypatch.setitem(sys.modules, "telethon", None)
    with pytest.raises(RuntimeError) as excinfo:
        session.build_client(make_settings())
    assert "Telethon" in str(excinfo.value)


def test_lock_accepts_tilde_path(tmp_path, monkeypatch):
    from telegram_live_call.session import SessionLock

    monkeypatch.setenv("HOME", str(tmp_path))
    lock = SessionLock("~/state/telegram")
    lock.acquire()
    try:
        assert (tmp_path / "state" / "telegram.session.lock").exists()
    finally:
        lock.release()
