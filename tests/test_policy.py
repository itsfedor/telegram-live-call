"""Tests use synthetic Telegram IDs; never real account information."""
import pytest


def test_policy_allows_only_owner_and_checks_source():
    from telegram_live_call.call_policy import CallPolicy
    policy = CallPolicy(owner_id=100000001, source_id=100000002)
    assert policy.authorize_target(100000001) == 100000001
    assert policy.authorize_source(100000002) == 100000002
    for target in [100000002, -100000001, '100000001', True, None]:
        with pytest.raises(PermissionError): policy.authorize_target(target)
    with pytest.raises(PermissionError): policy.authorize_source(100000001)


@pytest.mark.parametrize('owner,source', [(True,2),(1,1),(-1,2),(1,0),('1',2)])
def test_invalid_policy_is_rejected(owner,source):
    from telegram_live_call.call_policy import CallPolicy
    with pytest.raises(ValueError):CallPolicy(owner_id=owner,source_id=source)


@pytest.mark.asyncio
async def test_bounded_call_enforces_policy_and_hangs_up():
    from telegram_live_call.call_policy import CallPolicy
    from telegram_live_call.call_session import bounded_call
    class Calls:
        def __init__(self):self.actions=[]
        async def play(self,target,stream,config):self.actions.append(('play',target))
        async def leave_call(self,target):self.actions.append(('leave',target))
    calls=Calls()
    async def session():raise RuntimeError('test error')
    policy=CallPolicy(owner_id=100000001,source_id=100000002)
    with pytest.raises(RuntimeError):await bounded_call(calls,100000001,None,session,policy=policy,seconds=1)
    assert calls.actions==[('play',100000001),('leave',100000001)]
    calls.actions.clear()
    with pytest.raises(PermissionError):await bounded_call(calls,100000002,None,session,policy=policy)
    assert calls.actions==[]
