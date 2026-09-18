from __future__ import annotations

from enum import Enum
from typing import Dict, FrozenSet, Union


class CommandState(str, Enum):
    RECEIVED = "RECEIVED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    REJECTED = "REJECTED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    PENDING_RECONCILIATION = "PENDING_RECONCILIATION"
    OPERATOR_REQUIRED = "OPERATOR_REQUIRED"


class EffectState(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    RETRY_WAIT = "RETRY_WAIT"
    PENDING_RECONCILIATION = "PENDING_RECONCILIATION"
    DEAD_LETTER = "DEAD_LETTER"
    CANCELED = "CANCELED"


COMMAND_TRANSITIONS: Dict[CommandState, FrozenSet[CommandState]] = {
    CommandState.RECEIVED: frozenset(
        {CommandState.RUNNING, CommandState.REJECTED, CommandState.OPERATOR_REQUIRED}
    ),
    CommandState.RUNNING: frozenset(
        {
            CommandState.SUCCEEDED,
            CommandState.FAILED_RETRYABLE,
            CommandState.PENDING_RECONCILIATION,
            CommandState.OPERATOR_REQUIRED,
        }
    ),
    CommandState.FAILED_RETRYABLE: frozenset(
        {
            CommandState.RUNNING,
            CommandState.PENDING_RECONCILIATION,
            CommandState.OPERATOR_REQUIRED,
        }
    ),
    CommandState.PENDING_RECONCILIATION: frozenset(
        {CommandState.RUNNING, CommandState.OPERATOR_REQUIRED}
    ),
    CommandState.SUCCEEDED: frozenset(),
    CommandState.REJECTED: frozenset(),
    CommandState.OPERATOR_REQUIRED: frozenset(),
}

EFFECT_TRANSITIONS: Dict[EffectState, FrozenSet[EffectState]] = {
    EffectState.PENDING: frozenset({EffectState.READY, EffectState.CANCELED}),
    EffectState.READY: frozenset({EffectState.RUNNING, EffectState.CANCELED}),
    EffectState.RUNNING: frozenset(
        {
            EffectState.READY,
            EffectState.SUCCEEDED,
            EffectState.RETRY_WAIT,
            EffectState.PENDING_RECONCILIATION,
            EffectState.DEAD_LETTER,
        }
    ),
    EffectState.RETRY_WAIT: frozenset(
        {EffectState.READY, EffectState.RUNNING, EffectState.CANCELED}
    ),
    EffectState.PENDING_RECONCILIATION: frozenset(
        {EffectState.READY, EffectState.DEAD_LETTER, EffectState.CANCELED}
    ),
    EffectState.SUCCEEDED: frozenset(),
    EffectState.DEAD_LETTER: frozenset(),
    EffectState.CANCELED: frozenset(),
}


class InvalidStateTransition(ValueError):
    pass


State = Union[CommandState, EffectState]


def validate_transition(current: State, target: State) -> None:
    if type(current) is not type(target):
        raise InvalidStateTransition(f"state type mismatch: {type(current).__name__}")
    allowed = COMMAND_TRANSITIONS[current] if isinstance(current, CommandState) else EFFECT_TRANSITIONS[current]
    if target not in allowed:
        raise InvalidStateTransition(f"invalid transition: {current.value} -> {target.value}")
