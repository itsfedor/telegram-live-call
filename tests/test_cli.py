"""CLI: confirm gate, read-only check, login, greeting prepare, one bounded call."""
import asyncio
import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import SECRET_VALUES, FakeApp, FakePipe, SRC


def cli():
    from telegram_live_call import cli as module

    return module


def events(text):
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


def find(text, kind):
    found = [record for record in events(text) if record["event"] == kind]
    assert found, f"no {kind} event in {text}"
    return found[-1]


def mode_of(path):
    return stat.S_IMODE(os.stat(path).st_mode)


async def settle(rounds=40):
    for _ in range(rounds):
        await asyncio.sleep(0)


def _patch_settings(monkeypatch, module, settings):
    monkeypatch.setattr(module, "load_settings", lambda env_file=None: settings)


def _patch_pipeline(monkeypatch, module, env):
    monkeypatch.setattr(
        module, "_voice_pipeline_class", lambda: (lambda **kw: FakePipe(env, **kw))
    )


# ----------------------------------------------------------------- arguments #
def test_parse_defaults():
    args = cli().parse_args(["check"])

    assert args.command == "check"
    assert args.env_file is None


def test_parse_env_file_before_command_and_call_flags():
    args = cli().parse_args(
        ["--env-file", "/tmp/demo.env", "call", "--confirm", "--save-transcript"]
    )

    assert args.env_file == Path("/tmp/demo.env")
    assert args.command == "call"
    assert args.confirm is True
    assert args.save_transcript is True


def test_call_flags_default_to_off():
    args = cli().parse_args(["call"])

    assert args.confirm is False
    assert args.save_transcript is False


def test_help_lists_every_command(capsys):
    with pytest.raises(SystemExit):
        cli().parse_args(["--help"])

    out = capsys.readouterr().out
    for command in ("check", "login", "prepare", "call"):
        assert command in out
    assert "--env-file" in out


# --------------------------------------------------------------- confirm gate #
def test_call_without_confirm_refuses_before_settings_or_network(capsys, monkeypatch):
    module = cli()

    def explode(*args, **kwargs):
        raise AssertionError("nothing may load or connect before --confirm")

    monkeypatch.setattr(module, "load_settings", explode)

    assert module.main(["call"]) == 2

    record = find(capsys.readouterr().out, "refused")
    assert record["reason"] == "confirm_required"


# --------------------------------------------------------------------- check #
def test_check_reports_accounts_without_dial_or_tts(make_settings, monkeypatch, capsys):
    module = cli()
    settings = make_settings()
    _patch_settings(monkeypatch, module, settings)
    app = FakeApp(me_id=settings.call_source_id, entities={settings.call_owner_id: True})
    monkeypatch.setattr(module, "build_client", lambda config: app)
    monkeypatch.setattr(
        module, "_import_call_stack", lambda: pytest.fail("check must not import the media stack")
    )

    assert module.main(["check"]) == 0

    out = capsys.readouterr().out
    record = find(out, "check")
    assert record["ok"] is True
    assert record["source_id"] == settings.call_source_id
    assert record["owner_id"] == settings.call_owner_id
    assert record["dialed"] is False
    assert app.calls.count("connect") == 1
    assert "disconnect" in app.calls
    for secret in SECRET_VALUES:
        assert secret not in out


def test_check_rejects_wrong_source_account(make_settings, monkeypatch, capsys):
    module = cli()
    settings = make_settings()
    _patch_settings(monkeypatch, module, settings)
    app = FakeApp(me_id=settings.call_source_id + 1, entities={settings.call_owner_id: True})
    monkeypatch.setattr(module, "build_client", lambda config: app)

    assert module.main(["check"]) == 1

    assert find(capsys.readouterr().out, "error")["reason"] == "policy"
    assert "disconnect" in app.calls


def test_check_requires_authorized_session(make_settings, monkeypatch, capsys):
    module = cli()
    settings = make_settings()
    _patch_settings(monkeypatch, module, settings)
    app = FakeApp(me_id=settings.call_source_id, authorized=False)
    monkeypatch.setattr(module, "build_client", lambda config: app)

    assert module.main(["check"]) == 1

    record = find(capsys.readouterr().out, "error")
    assert record["reason"] == "not_authorized"
    assert "login" in record["message"]


def test_check_fails_closed_when_session_is_busy(make_settings, monkeypatch, capsys):
    module = cli()
    settings = make_settings()
    _patch_settings(monkeypatch, module, settings)

    def busy(config):
        raise module.SessionBusy("session held elsewhere")

    monkeypatch.setattr(module, "acquire_session_lock", busy)
    monkeypatch.setattr(module, "build_client", lambda config: pytest.fail("must not connect"))

    assert module.main(["check"]) == 1

    assert find(capsys.readouterr().out, "error")["reason"] == "session_busy"


def test_lock_is_acquired_before_connect_and_released_after_disconnect(
    make_settings, monkeypatch
):
    module = cli()
    settings = make_settings()
    _patch_settings(monkeypatch, module, settings)
    timeline = []

    class Lock:
        def release(self):
            timeline.append("lock_release")

    def acquire(config):
        timeline.append("lock_acquire")
        return Lock()

    monkeypatch.setattr(module, "acquire_session_lock", acquire)
    app = FakeApp(
        me_id=settings.call_source_id,
        entities={settings.call_owner_id: True},
        timeline=timeline,
    )
    monkeypatch.setattr(module, "build_client", lambda config: app)

    assert module.main(["check"]) == 0
    assert timeline[0] == "lock_acquire"
    assert timeline.index("lock_acquire") < timeline.index("connect")
    assert timeline.index("disconnect") < timeline.index("lock_release")


# ----------------------------------------------------------------- owner look #
def test_owner_resolution_falls_back_to_one_dialog_fetch(make_settings):
    module = cli()
    settings = make_settings()
    policy = module.build_policy(settings)
    app = FakeApp(
        me_id=settings.call_source_id, entities={}, dialogs_fill={settings.call_owner_id: True}
    )

    entity = asyncio.run(
        module.resolve_owner(app, settings, policy, peer_user=lambda uid: ("peer", uid))
    )

    assert entity.id == settings.call_owner_id
    assert app.dialogs_calls == 1
    assert ("get_dialogs", None) in app.calls


def test_owner_username_is_only_a_fallback_and_id_is_verified(make_settings):
    module = cli()
    settings = make_settings(CALL_OWNER_USERNAME="some_owner")
    policy = module.build_policy(settings)
    good = FakeApp(me_id=settings.call_source_id, entities={}, username_id=settings.call_owner_id)

    entity = asyncio.run(
        module.resolve_owner(good, settings, policy, peer_user=lambda uid: ("peer", uid))
    )

    assert entity.id == settings.call_owner_id
    assert "get_entity_username" in good.calls

    wrong = FakeApp(
        me_id=settings.call_source_id, entities={}, username_id=settings.call_owner_id + 1
    )
    with pytest.raises(PermissionError):
        asyncio.run(
            module.resolve_owner(wrong, settings, policy, peer_user=lambda uid: ("peer", uid))
        )


def test_owner_resolution_fails_closed_when_unknown(make_settings):
    module = cli()
    settings = make_settings()
    policy = module.build_policy(settings)
    app = FakeApp(me_id=settings.call_source_id, entities={})

    with pytest.raises(module.CliError) as excinfo:
        asyncio.run(
            module.resolve_owner(app, settings, policy, peer_user=lambda uid: ("peer", uid))
        )

    assert excinfo.value.reason == "owner_unresolved"


# ------------------------------------------------------------------- prepare #
def test_prepare_synthesizes_greeting_into_private_state_dir(
    make_settings, fake_env, monkeypatch, capsys
):
    module = cli()
    settings = make_settings()
    _patch_settings(monkeypatch, module, settings)
    _patch_pipeline(monkeypatch, module, fake_env)

    assert module.main(["prepare"]) == 0

    cache = settings.greeting_cache_path()
    assert cache.is_file()
    assert mode_of(cache) == 0o600
    assert cache.read_bytes() == fake_env.synth_pcm
    assert len(cache.read_bytes()) % 2 == 0
    assert fake_env.synth_texts == [settings.greeting]
    assert fake_env.pipes[0].stopped is True
    assert find(capsys.readouterr().out, "prepared")["bytes"] == len(fake_env.synth_pcm)


def test_prepare_reuses_cached_greeting(make_settings, fake_env, monkeypatch):
    module = cli()
    settings = make_settings()
    _patch_settings(monkeypatch, module, settings)
    _patch_pipeline(monkeypatch, module, fake_env)

    assert module.main(["prepare"]) == 0
    assert module.main(["prepare"]) == 0

    assert len(fake_env.pipes) == 1


def test_prepare_rejects_unusable_pcm(make_settings, fake_env, monkeypatch, capsys):
    module = cli()
    settings = make_settings()
    _patch_settings(monkeypatch, module, settings)
    _patch_pipeline(monkeypatch, module, fake_env)
    fake_env.synth_pcm = b"\x01\x00\x02"

    assert module.main(["prepare"]) == 1

    assert not settings.greeting_cache_path().exists()
    assert find(capsys.readouterr().out, "error")["reason"] == "invalid_pcm"


# ---------------------------------------------------------- call via main() #
def _seed_greeting(settings, pcm=b"\x02\x00" * 32):
    from telegram_live_call.session import ensure_dir, ensure_private

    ensure_dir(settings.state_dir)
    cache = settings.greeting_cache_path()
    cache.write_bytes(pcm)
    ensure_private(cache)
    return pcm


def _run_call_command(module, settings, fake_env, fake_stack, *, extra=None, prepare=False,
                      monkeypatch):
    # The fake call ends itself through the "remote hung up" update.
    fake_env.auto_hangup = True
    fake_env.owner_id = settings.call_owner_id
    if prepare:
        _patch_pipeline(monkeypatch, module, fake_env)
    else:
        if not settings.greeting_cache_path().exists():
            _seed_greeting(settings)
        monkeypatch.setattr(
            module,
            "_voice_pipeline_class",
            lambda: pytest.fail("a cached greeting must not be re-synthesized"),
        )
    monkeypatch.setattr(module, "_import_call_stack", lambda: fake_stack)

    class Lock:
        def release(self):
            fake_env.timeline.append("lock_release")

    monkeypatch.setattr(
        module,
        "acquire_session_lock",
        lambda config: (fake_env.timeline.append("lock_acquire"), Lock())[1],
    )
    app = FakeApp(
        me_id=settings.call_source_id,
        entities={settings.call_owner_id: True},
        timeline=fake_env.timeline,
    )
    monkeypatch.setattr(module, "build_client", lambda config: app)
    return module.main(["call", "--confirm", *(extra or [])]), app


def test_call_prepares_missing_greeting_before_dialing(
    make_settings, fake_env, fake_stack, monkeypatch
):
    module = cli()
    settings = make_settings()
    _patch_settings(monkeypatch, module, settings)

    code, _app = _run_call_command(
        module, settings, fake_env, fake_stack, prepare=True, monkeypatch=monkeypatch
    )

    assert code == 0
    assert fake_env.timeline.index("synthesize") < fake_env.timeline.index("bounded_call")
    assert mode_of(settings.greeting_cache_path()) == 0o600
    assert fake_env.bounded_calls[0]["target"] == settings.call_owner_id


def test_call_fails_closed_when_session_is_busy(make_settings, monkeypatch, capsys):
    module = cli()
    settings = make_settings()
    _patch_settings(monkeypatch, module, settings)
    monkeypatch.setattr(
        module,
        "acquire_session_lock",
        lambda config: (_ for _ in ()).throw(module.SessionBusy("session held elsewhere")),
    )
    monkeypatch.setattr(module, "build_client", lambda config: pytest.fail("must not connect"))
    monkeypatch.setattr(
        module, "_import_call_stack", lambda: pytest.fail("must not import the media stack")
    )

    assert module.main(["call", "--confirm"]) == 1

    assert find(capsys.readouterr().out, "error")["reason"] == "session_busy"


def test_call_rejects_wrong_source_account(make_settings, monkeypatch, capsys):
    module = cli()
    settings = make_settings()
    _patch_settings(monkeypatch, module, settings)
    app = FakeApp(me_id=settings.call_source_id + 7)
    monkeypatch.setattr(module, "build_client", lambda config: app)
    monkeypatch.setattr(
        module, "_import_call_stack", lambda: pytest.fail("must not import the media stack")
    )

    assert module.main(["call", "--confirm"]) == 1

    assert find(capsys.readouterr().out, "error")["reason"] == "policy"
    assert "disconnect" in app.calls


def test_call_uses_bounded_call_with_policy_and_configured_seconds(
    make_settings, fake_env, fake_stack, monkeypatch
):
    module = cli()
    settings = make_settings(CALL_MAX_SECONDS="45")
    _patch_settings(monkeypatch, module, settings)

    code, _app = _run_call_command(module, settings, fake_env, fake_stack, monkeypatch=monkeypatch)

    assert code == 0
    bounded = fake_env.bounded_calls[0]
    assert bounded["seconds"] == 45
    assert bounded["target"] == settings.call_owner_id
    assert bounded["stream"] == ("media", "external-audio", ("params", {"bitrate": 48000, "channels": 1}))


def test_call_starts_asr_and_recording_only_after_connect(
    make_settings, fake_env, fake_stack, monkeypatch
):
    module = cli()
    settings = make_settings()
    _patch_settings(monkeypatch, module, settings)

    assert _run_call_command(module, settings, fake_env, fake_stack, monkeypatch=monkeypatch)[0] == 0

    assert fake_env.calls.started is True
    assert fake_env.timeline.index("bounded_call") < fake_env.timeline.index("pipe_start")
    assert fake_env.calls.recorded == [settings.call_owner_id]
    assert fake_env.pipes[0].started is True


def test_call_plays_cached_greeting_first(make_settings, fake_env, fake_stack, monkeypatch):
    module = cli()
    settings = make_settings()
    _patch_settings(monkeypatch, module, settings)
    greeting = _seed_greeting(settings, b"\x03\x00" * 16)

    assert _run_call_command(module, settings, fake_env, fake_stack, monkeypatch=monkeypatch)[0] == 0

    assert fake_env.outputs[0].played[0] == greeting
    assert fake_env.synth_texts == []


def test_call_pipeline_is_configured_with_voice_and_speech_limits(
    make_settings, fake_env, fake_stack, monkeypatch
):
    module = cli()
    settings = make_settings()
    _patch_settings(monkeypatch, module, settings)

    assert _run_call_command(module, settings, fake_env, fake_stack, monkeypatch=monkeypatch)[0] == 0

    kwargs = fake_env.pipes[0].kwargs
    assert kwargs["voice_id"] == settings.elevenlabs_voice_id
    assert kwargs["chat_model"] == settings.chat_model
    assert kwargs["tts_model"] == settings.tts_model
    assert kwargs["system_context"] == settings.system_context()
    assert kwargs["max_speech_chars"] == 400
    assert kwargs["speech_budget_chars"] == 2000
    assert kwargs["request_timeout"] == 20
    assert callable(kwargs["on_transcript"])


def test_call_without_transcript_flag_prints_no_speech_text(
    make_settings, fake_env, fake_stack, monkeypatch, capsys
):
    module = cli()
    settings = make_settings()
    _patch_settings(monkeypatch, module, settings)
    fake_env.auto_utterances = ["до свидания"]
    fake_env.replies = ["хорошо, завершаю"]

    assert _run_call_command(module, settings, fake_env, fake_stack, monkeypatch=monkeypatch)[0] == 0

    out = capsys.readouterr().out
    assert "до свидания" not in out
    assert "хорошо, завершаю" not in out
    assert "text_chars" in out
    assert not list(settings.state_dir.glob("call-*.jsonl"))


def test_call_with_save_transcript_writes_private_log(
    make_settings, fake_env, fake_stack, monkeypatch, capsys
):
    module = cli()
    settings = make_settings()
    _patch_settings(monkeypatch, module, settings)
    fake_env.auto_utterances = ["до свидания"]
    fake_env.replies = ["хорошо, завершаю"]

    code, _app = _run_call_command(
        module, settings, fake_env, fake_stack, extra=["--save-transcript"], monkeypatch=monkeypatch
    )

    assert code == 0
    logs = list(settings.state_dir.glob("call-*.jsonl"))
    assert len(logs) == 1
    assert mode_of(logs[0]) == 0o600
    assert "до свидания" in logs[0].read_text(encoding="utf-8")
    assert "до свидания" in capsys.readouterr().out


# ------------------------------------------------------- call orchestration #
def _drive(module, settings, fake_env, fake_stack, *, greeting=b"\x04\x00" * 16):
    """Start run_call inside a loop, hand the live fakes to ``body``, return events + metrics."""

    async def scenario(body):
        app = FakeApp(
            me_id=settings.call_source_id,
            entities={settings.call_owner_id: True},
            timeline=fake_env.timeline,
        )
        policy = module.build_policy(settings)
        stop = asyncio.Event()
        stream = io.StringIO()
        log = module.EventLog(stream=stream, secrets=(settings.telegram_api_hash,))
        task = asyncio.create_task(
            module.run_call(
                app, settings, policy, stack=fake_stack, greeting=greeting, log=log, stop=stop
            )
        )
        await settle()
        try:
            await body(fake_env, stream, stop, task)
        finally:
            stop.set()
            if not task.done():
                await asyncio.wait_for(task, timeout=3)
        metrics = task.result() if task.done() and not task.cancelled() else {}
        return stream.getvalue(), metrics

    return scenario


def test_interruption_discards_stale_reply(make_settings, fake_env, fake_stack):
    module = cli()
    settings = make_settings()

    async def body(env, stream, stop, task):
        pipe = env.pipes[0]
        env.respond_gate = asyncio.Event()
        env.replies = ["устаревший ответ", "свежий ответ"]
        pipe.on_transcript("первая фраза")
        await settle()
        pipe.on_speech_started()
        env.respond_gate.set()
        await settle()
        assert "устаревший ответ" not in env.synth_texts
        pipe.on_transcript("вторая фраза")
        await settle()
        assert env.synth_texts[-1] == "свежий ответ"
        assert env.outputs[0].played[-1] == env.synth_pcm

    text, metrics = asyncio.run(_drive(module, settings, fake_env, fake_stack)(body))

    assert metrics["interruptions"] >= 1
    assert metrics["stale_replies"] >= 1
    assert find(text, "ended")["answers"] == 1


def test_farewell_utterance_ends_the_call_cleanly(make_settings, fake_env, fake_stack):
    module = cli()
    settings = make_settings()

    async def body(env, stream, stop, task):
        env.replies = ["хорошо, до связи"]
        env.pipes[0].on_transcript("до свидания")
        await asyncio.wait_for(task, timeout=3)
        assert "bounded_return" in env.timeline
        assert env.pipes[0].stopped is True

    text, metrics = asyncio.run(_drive(module, settings, fake_env, fake_stack)(body))

    assert metrics["answers"] == 1
    assert find(text, "ended")["transcripts"] == 1


def test_queue_overflow_fails_loud(make_settings, fake_env, fake_stack):
    module = cli()
    settings = make_settings()

    async def body(env, stream, stop, task):
        pipe = env.pipes[0]
        env.respond_gate = asyncio.Event()
        pipe.on_transcript("первая")
        await settle()
        for index in range(11):
            pipe.on_transcript(f"лишняя {index}")
        await asyncio.wait_for(task, timeout=3)
        assert "utterance_queue_full" in stream.getvalue()

    text, metrics = asyncio.run(_drive(module, settings, fake_env, fake_stack)(body))

    assert metrics["transcripts"] == 12


def test_frames_from_other_chats_are_ignored(make_settings, fake_env, fake_stack):
    module = cli()
    settings = make_settings()
    pcm = b"\x05\x00" * 960

    async def body(env, stream, stop, task):
        pipe = env.pipes[0]
        incoming = env.calls.handlers["incoming"]
        await incoming(
            None,
            SimpleNamespace(
                chat_id=settings.call_owner_id + 1, frames=[SimpleNamespace(frame=pcm)]
            ),
        )
        assert pcm not in pipe.fed
        await incoming(
            None,
            SimpleNamespace(chat_id=settings.call_owner_id, frames=[SimpleNamespace(frame=pcm)]),
        )
        assert pcm in pipe.fed
        assert env.bounded_calls[0]["target"] == settings.call_owner_id

    asyncio.run(_drive(module, settings, fake_env, fake_stack)(body))


def test_signal_stop_exits_gracefully_and_stops_asr(make_settings, fake_env, fake_stack):
    module = cli()
    settings = make_settings()

    async def body(env, stream, stop, task):
        stop.set()
        await asyncio.wait_for(task, timeout=3)
        assert "bounded_return" in env.timeline
        assert env.pipes[0].stopped is True

    text, metrics = asyncio.run(_drive(module, settings, fake_env, fake_stack)(body))

    assert find(text, "stopped")["reason"] == "signal"


def test_remote_hangup_ends_the_call(make_settings, fake_env, fake_stack):
    module = cli()
    settings = make_settings()

    async def body(env, stream, stop, task):
        ended = env.calls.handlers["status"]
        await ended(None, SimpleNamespace(chat_id=settings.call_owner_id, status="discarded"))
        await asyncio.wait_for(task, timeout=3)
        assert "remote_ended" in stream.getvalue()

    asyncio.run(_drive(module, settings, fake_env, fake_stack)(body))


def test_asr_failure_ends_the_call(make_settings, fake_env, fake_stack):
    module = cli()
    settings = make_settings()

    async def body(env, stream, stop, task):
        env.pipes[0].last_error = RuntimeError("deepgram closed")
        await asyncio.wait_for(task, timeout=5)
        assert "deepgram" in stream.getvalue()

    asyncio.run(_drive(module, settings, fake_env, fake_stack)(body))


# -------------------------------------------------------------------- events #
def test_events_redact_secrets_and_hide_speech_by_default(make_settings):
    module = cli()
    settings = make_settings()
    buffer = io.StringIO()
    log = module.EventLog(stream=buffer, secrets=(settings.telegram_api_hash,))

    record = log.event("user", text="секретная фраза", note=f"leak {settings.telegram_api_hash}")

    out = buffer.getvalue()
    assert "секретная фраза" not in out
    assert settings.telegram_api_hash not in out
    assert "[redacted]" in out
    assert record["text_chars"] == len("секретная фраза")


def test_transcript_file_is_private_and_keeps_text(tmp_path):
    module = cli()
    path = tmp_path / "state" / "call-test.jsonl"
    buffer = io.StringIO()
    log = module.EventLog(stream=buffer, transcript_path=path)
    try:
        log.event("user", text="фраза в файл")
    finally:
        log.close()

    assert "фраза в файл" in path.read_text(encoding="utf-8")
    assert "фраза в файл" in buffer.getvalue()
    assert mode_of(path) == 0o600


def test_signal_handlers_are_registered_and_removed():
    module = cli()
    stop = asyncio.Event()
    registered = {}

    class FakeLoop:
        def add_signal_handler(self, sig, callback, *args):
            registered[sig] = callback

        def remove_signal_handler(self, sig):
            registered.pop(sig, None)
            return True

    async def body():
        loop = FakeLoop()
        with module.install_signal_handlers(stop, loop=loop) as installed:
            assert installed
            registered[module.signal.SIGTERM]()
            assert stop.is_set() is True
        assert registered == {}

    asyncio.run(body())


# --------------------------------------------------------------------- login #
def test_login_secures_session_file_and_reports_source(make_settings, monkeypatch, capsys):
    from telegram_live_call.session import ensure_dir, session_file_path

    module = cli()
    settings = make_settings()
    _patch_settings(monkeypatch, module, settings)
    ensure_dir(settings.session_path.parent)
    session_file = session_file_path(settings.session_path)
    session_file.write_bytes(b"sqlite")
    os.chmod(session_file, 0o644)
    app = FakeApp(me_id=settings.call_source_id, authorized=False)
    monkeypatch.setattr(module, "build_client", lambda config: app)

    assert module.main(["login"]) == 0

    assert "start" in app.calls
    assert mode_of(session_file) == 0o600
    out = capsys.readouterr().out
    record = find(out, "login")
    assert record["ok"] is True
    assert record["source_id"] == settings.call_source_id
    assert "session_string" not in out
    for secret in SECRET_VALUES:
        assert secret not in out


def test_login_rejects_wrong_account(make_settings, monkeypatch, capsys):
    module = cli()
    settings = make_settings()
    _patch_settings(monkeypatch, module, settings)
    app = FakeApp(me_id=settings.call_source_id + 5)
    monkeypatch.setattr(module, "build_client", lambda config: app)

    assert module.main(["login"]) == 1

    assert find(capsys.readouterr().out, "error")["reason"] == "policy"


# ------------------------------------------------------------ import weight #
def test_help_and_check_do_not_import_media_stack():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, telegram_live_call.cli;"
            "print('pytgcalls' in sys.modules, 'telethon' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(SRC)},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["False", "False"]
