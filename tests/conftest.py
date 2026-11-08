"""Shared fixtures for the public telegram-live-call CLI tests.

Everything here is sanitized: fake API keys, fake user IDs, temporary HOME.
No network, no Telegram, no provider calls.
"""
import os
import sys
from pathlib import Path
import asyncio
from types import SimpleNamespace

import pytest

def _resolve_src() -> Path:
    """Locate the package: $TLC_SRC, then src/ next to the tests directory, then cwd."""
    candidates = []
    if os.environ.get("TLC_SRC"):
        candidates.append(Path(os.environ["TLC_SRC"]))
    here = Path(__file__).resolve().parent
    candidates += [
        here.parent / "src",
        here.parent.parent / "src",
        Path.cwd() / "src",
        Path.cwd() / "telegram-live-call-public" / "src",
    ]
    candidates += [Path(entry) for entry in sys.path if entry]
    for candidate in candidates:
        if (candidate / "telegram_live_call" / "__init__.py").is_file():
            return candidate
    raise RuntimeError("point TLC_SRC at the telegram-live-call-public/src directory")


SRC = _resolve_src()
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

SANITIZED_ENV = {
    "TELEGRAM_API_ID": "123456",
    "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef",
    "CALL_OWNER_ID": "100200300",
    "CALL_SOURCE_ID": "400500600",
    "DEEPGRAM_API_KEY": "dg-sanitized-key",
    "ELEVENLABS_API_KEY": "el-sanitized-key",
    "ELEVENLABS_VOICE_ID": "voice-sanitized-id",
    "OPENROUTER_API_KEY": "or-sanitized-key",
}

SECRET_VALUES = (
    SANITIZED_ENV["TELEGRAM_API_HASH"],
    SANITIZED_ENV["DEEPGRAM_API_KEY"],
    SANITIZED_ENV["ELEVENLABS_API_KEY"],
    SANITIZED_ENV["OPENROUTER_API_KEY"],
    SANITIZED_ENV["ELEVENLABS_VOICE_ID"],
)


def write_dotenv(path: Path, values) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    return path


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def env_file(tmp_path, fake_home):
    return write_dotenv(tmp_path / "call.env", SANITIZED_ENV)


@pytest.fixture
def make_settings(tmp_path, fake_home):
    """Build real Settings through from_env (dotenv file only)."""

    def build(**overrides):
        from telegram_live_call.settings import from_env

        values = dict(SANITIZED_ENV)
        values.update({k: v for k, v in overrides.items() if v is not None})
        for key, value in overrides.items():
            if value is None:
                values.pop(key, None)
        state = tmp_path / "state"
        path = write_dotenv(tmp_path / "call.env", {**values, "STATE_DIR": str(state)})
        return from_env(path, environ={})

    return build


# --------------------------------------------------------------------------- #
# Fake call stack (audio_output / call_session / voice_pipeline / pytgcalls)
# --------------------------------------------------------------------------- #
class FakeEnv:
    """Mutable behaviour knobs shared by the fakes."""

    def __init__(self):
        self.timeline = []
        self.replies = []
        self.synth_pcm = b"\x01\x00" * 64
        self.synth_texts = []
        self.auto_utterances = []     # delivered to on_transcript when ASR starts
        self.auto_hangup = False      # fire the remote-ended update once ASR starts
        self.owner_id = None
        self.respond_gate = None      # asyncio.Event blocking respond()
        self.incoming = None          # handler for stream frames
        self.status = None            # handler for call status updates
        self.calls = None
        self.pipes = []
        self.outputs = []
        self.bounded_calls = []
        self.peer_user = lambda uid: ("peer", uid)


class FakePipe:
    def __init__(self, env, **kwargs):
        self.env = env
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.fed = []
        self.last_error = None
        self.dropped_frames = 0
        self.speech_chars_used = 0
        self.greeting_used = False
        self.on_transcript = kwargs.get("on_transcript")
        self.on_interim = kwargs.get("on_interim")
        self.on_speech_started = kwargs.get("on_speech_started")
        env.pipes.append(self)

    async def start(self):
        self.started = True
        self.env.timeline.append("pipe_start")
        for text in list(self.env.auto_utterances):
            if self.on_transcript is not None:
                self.on_transcript(text)
        if self.env.auto_hangup:
            asyncio.create_task(self._auto_hangup())

    async def _auto_hangup(self):
        await asyncio.sleep(0.01)
        handler = (self.env.calls.handlers if self.env.calls else {}).get("status")
        if handler is not None:
            await handler(
                None,
                SimpleNamespace(chat_id=self.env.owner_id, status="discarded"),
            )

    async def feed_audio(self, pcm):
        self.fed.append(pcm)
        return 1

    async def respond(self, history):
        self.env.timeline.append("respond")
        if self.env.respond_gate is not None:
            await self.env.respond_gate.wait()
        return self.env.replies.pop(0) if self.env.replies else "хорошо"

    async def synthesize(self, text):
        self.env.timeline.append("synthesize")
        self.env.synth_texts.append(text)
        return self.env.synth_pcm

    async def stop(self):
        self.stopped = True
        self.env.timeline.append("pipe_stop")


class FakeOutput:
    def __init__(self, env, send):
        self.env = env
        self.send = send
        self.generation = 0
        self.played = []
        env.outputs.append(self)

    def interrupt(self):
        self.generation += 1
        self.env.timeline.append("interrupt")

    async def play(self, pcm):
        self.played.append(pcm)
        self.env.timeline.append("play")


class FakeCalls:
    def __init__(self, env, app):
        self.env = env
        self.app = app
        self.handlers = {}
        self.started = False
        self.recorded = []
        self.frames = []
        self.left = 0
        self.played = []

    def on_update(self, key):
        def register(fn):
            self.handlers[key] = fn
            return fn

        return register

    async def start(self):
        self.started = True
        self.env.timeline.append("calls_start")

    async def play(self, target, stream, config=None):
        self.played.append(target)
        self.env.timeline.append("calls_play")

    async def send_frame(self, chat, device, frame):
        self.frames.append((chat, device, len(frame)))

    async def record(self, chat, stream):
        self.recorded.append(chat)
        self.env.timeline.append("record")

    async def leave_call(self, chat):
        self.left += 1
        self.env.timeline.append("leave")


class FakeStack:
    """Stand-in for cli.CallStack; every heavy dependency is a sanitized fake."""

    def __init__(self, env):
        self.env = env
        self.filters = SimpleNamespace(
            stream_frame=lambda *a, **k: "incoming",
            chat_update=lambda *a, **k: "status",
        )
        self.ChatUpdate = SimpleNamespace(
            Status=SimpleNamespace(DISCARDED_CALL=1, BUSY_CALL=2)
        )
        self.Device = SimpleNamespace(MICROPHONE="mic")
        self.Direction = SimpleNamespace(INCOMING="in")
        self.ExternalMedia = SimpleNamespace(AUDIO="external-audio")
        self.MediaStream = lambda kind, params: ("media", kind, params)
        self.RecordStream = lambda flag, params: ("record", flag, params)
        self.AudioParameters = lambda **kwargs: ("params", kwargs)
        self.AudioOutput = lambda send: FakeOutput(env, send)
        self.VoicePipeline = lambda **kwargs: FakePipe(env, **kwargs)
        self.peer_user = env.peer_user
        self._env_for_calls = env

        async def bounded_call(calls, target, stream, session, seconds=60, *, policy):
            policy.authorize_target(target)
            env.bounded_calls.append(
                {"calls": calls, "target": target, "stream": stream, "seconds": seconds}
            )
            env.timeline.append("bounded_call")
            try:
                return await session()
            finally:
                env.timeline.append("bounded_return")

        self.bounded_call = bounded_call

    def py_tg_calls(self, app):
        self.env.calls = FakeCalls(self.env, app)
        self.env.timeline.append("calls_init")
        return self.env.calls


@pytest.fixture
def fake_env():
    return FakeEnv()


@pytest.fixture
def fake_stack(fake_env):
    stack = FakeStack(fake_env)

    class _Factory:
        def __init__(self, target):
            self.target = target

        def __call__(self, app):
            return self.target.py_tg_calls(app)

    stack.PyTgCalls = _Factory(stack)
    return stack


class FakeApp:
    """Minimal Telegram client double."""

    def __init__(self, *, me_id, owner_id=None, authorized=True, entities=None,
                 dialogs_fill=None, username_id=None, timeline=None):
        self.me_id = me_id
        self.owner_id = owner_id
        self.authorized = authorized
        self.entities = dict(entities or {})
        self.dialogs_fill = dialogs_fill
        self.username_id = username_id
        self.timeline = timeline
        self.calls = []
        self.dialogs_calls = 0
        self.started = False

    def _record(self, name):
        self.calls.append(name)
        if self.timeline is not None:
            self.timeline.append(name)

    async def connect(self):
        self._record("connect")

    async def disconnect(self):
        self._record("disconnect")

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return SimpleNamespace(id=self.me_id)

    async def get_entity(self, target):
        uid = target[1] if isinstance(target, tuple) else getattr(target, "user_id", None)
        if uid is not None:
            if uid in self.entities:
                self._record("get_entity_peer")
                return SimpleNamespace(id=uid)
            raise ValueError("could not find the input entity")
        if isinstance(target, str) and self.username_id is not None:
            self._record("get_entity_username")
            return SimpleNamespace(id=self.username_id)
        raise ValueError("could not find the input entity")

    async def get_dialogs(self, limit=None):
        self._record(("get_dialogs", limit))
        self.dialogs_calls += 1
        if self.dialogs_fill:
            self.entities.update(self.dialogs_fill)

    async def start(self):
        self._record("start")
        self.started = True
        return self
