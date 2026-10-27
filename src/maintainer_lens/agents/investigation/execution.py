"""Timeout and observability controls for investigation tool execution."""

import asyncio
import logging
import math
import time

from maintainer_lens.agents.investigation.state import InvestigationState
from maintainer_lens.agents.investigation.tool_calling import (
    SearchToolStepResult,
)
from maintainer_lens.agents.investigation.tool_node import ToolSearchRunner


def with_tool_step_controls(
    runner: ToolSearchRunner,
    *,
    timeout_seconds: float = 120.0,
    logger: logging.Logger | None = None,
) -> ToolSearchRunner:
    """Add a bounded deadline and safe execution logging to a tool runner."""

    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("timeout_seconds must be finite and positive")

    event_logger = logger if logger is not None else logging.getLogger(__name__)

    async def controlled_run(
        state: InvestigationState,
    ) -> SearchToolStepResult:
        started_at = time.perf_counter()
        timeout_scope = asyncio.timeout(timeout_seconds)

        result: SearchToolStepResult | None = None
        outcome = "failed"
        error_type: str | None = None

        try:
            async with timeout_scope:
                result = await runner(state)

            outcome = "skipped" if result["skipped_duplicate"] else "succeeded"
            return result

        except TimeoutError:
            outcome = "timed_out" if timeout_scope.expired() else "failed"
            error_type = "TimeoutError"
            raise

        except asyncio.CancelledError:
            outcome = "cancelled"
            error_type = "CancelledError"
            raise

        except Exception as exc:
            outcome = "failed"
            error_type = type(exc).__name__
            raise

        finally:
            duration_ms = round(
                (time.perf_counter() - started_at) * 1000,
                2,
            )

            tool_name = result["tool_name"] if result is not None else None

            level = (
                logging.WARNING if outcome in ("failed", "timed_out") else logging.INFO
            )

            event_logger.log(
                level,
                (
                    "Investigation tool step %s "
                    "(round=%s, tool=%s, duration_ms=%.2f, error=%s)"
                ),
                outcome,
                state.get("search_round"),
                tool_name,
                duration_ms,
                error_type,
                extra={
                    "event": "investigation_tool_step",
                    "outcome": outcome,
                    "search_round": state.get("search_round"),
                    "tool_name": tool_name,
                    "duration_ms": duration_ms,
                    "error_type": error_type,
                },
            )

    return controlled_run
