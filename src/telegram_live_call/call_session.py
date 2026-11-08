"""A single outbound call, hard time limit, guaranteed best-effort hangup."""
import asyncio
from .call_policy import CallPolicy
from pytgcalls.types import CallConfig


async def bounded_call(calls, target, stream, session, seconds=60, *, policy: CallPolicy):
    policy.authorize_target(target)
    if not 0 < seconds <= 300:
        raise ValueError('Call duration must be in (0, 300] seconds')
    try:
        async with asyncio.timeout(seconds):
            await calls.play(target, stream, config=CallConfig(timeout=30))
            return await session()
    finally:
        try:
            await asyncio.wait_for(calls.leave_call(target), timeout=5)
        except Exception:
            pass
