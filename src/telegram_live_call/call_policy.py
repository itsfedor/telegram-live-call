"""Fail-closed policy: one configured owner and a separate assistant account."""
from dataclasses import dataclass


@dataclass(frozen=True)
class CallPolicy:
    owner_id: int
    source_id: int

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in (self.owner_id,self.source_id)):
            raise ValueError('Owner and source must be positive integer user IDs')
        if self.owner_id == self.source_id:
            raise ValueError('Use a separate assistant account, not the owner account')

    def authorize_target(self,target: int) -> int:
        if type(target) is not int or target != self.owner_id:
            raise PermissionError('Only the configured owner may be called')
        return target

    def authorize_source(self,source: int) -> int:
        if type(source) is not int or source != self.source_id:
            raise PermissionError('Unexpected Telegram source account')
        return source
