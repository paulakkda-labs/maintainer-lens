"""Optional node-boundary observation without changing Graph state updates."""

import inspect
from collections.abc import Awaitable, Callable

from maintainer_lens.agents.investigation.state import InvestigationState

type NodeObserver = Callable[[str, InvestigationState], Awaitable[None]]


def observe_node(name: str, action, observer: NodeObserver | None):
    if observer is None:
        return action

    async def execute(state: InvestigationState):
        await observer(name, state)
        result = action(state)
        return await result if inspect.isawaitable(result) else result

    return execute
