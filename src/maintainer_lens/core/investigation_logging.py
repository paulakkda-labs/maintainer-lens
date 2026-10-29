"""Task-scoped, bounded diagnostics without exception text or captured values."""

import json
import logging
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

logger = logging.getLogger(__name__)


@dataclass
class _InvestigationLogContext:
    task_id: str
    repository: str
    issue_number: int
    generation: int
    phase: str = "starting"
    search_round: int | None = None


# Descendant Graph/TaskGroup tasks share this delivery's last committed phase.
# Concurrent deliveries get distinct objects; the token restores the outer scope.
_context: ContextVar[_InvestigationLogContext | None] = ContextVar(
    "investigation_log_context", default=None
)
_VALIDATION_FIELDS = frozenset(
    {
        "summary",
        "findings",
        "statement",
        "evidence_ids",
        "hypotheses",
        "unanswered_questions",
    }
)
_VALIDATION_TYPES = frozenset(
    {
        "missing",
        "too_short",
        "too_long",
        "string_type",
        "string_too_short",
        "string_too_long",
        "list_type",
        "dict_type",
        "model_type",
        "extra_forbidden",
        "value_error",
        "json_invalid",
        "literal_error",
        "int_type",
        "bool_type",
    }
)


@contextmanager
def investigation_log_context(
    *, task_id: str, repository: str, issue_number: int, generation: int
) -> Iterator[None]:
    """Bind one delivery, including its awaited descendants, without any I/O."""
    token = _context.set(
        _InvestigationLogContext(task_id, repository, issue_number, generation)
    )
    try:
        yield
    finally:
        _context.reset(token)


def _error_diagnostics(error: BaseException) -> dict:
    """Copy code locations and allowlisted validation paths, never str/repr."""
    seen: set[int] = set()

    def describe(exc: BaseException, depth: int) -> dict:
        if id(exc) in seen or len(seen) >= 20 or depth >= 5:
            return {"truncated": True}
        seen.add(id(exc))
        frames: deque[dict] = deque(maxlen=8)
        tb = exc.__traceback__
        while tb is not None:
            code = tb.tb_frame.f_code
            frames.append(
                {
                    "file": Path(code.co_filename).name,
                    "function": code.co_name,
                    "line": tb.tb_lineno,
                }
            )
            tb = tb.tb_next
        result = {"type": type(exc).__name__, "frames": list(frames)}
        if isinstance(exc, ValidationError):
            errors = exc.errors(
                include_input=False, include_context=False, include_url=False
            )
            result["validation"] = [
                {
                    "location": [
                        part
                        if (type(part) is int and 0 <= part <= 100000)
                        or (isinstance(part, str) and part in _VALIDATION_FIELDS)
                        else "<field>"
                        for part in item["loc"][:8]
                    ],
                    "type": item["type"]
                    if item["type"] in _VALIDATION_TYPES
                    else "validation_error",
                }
                for item in errors[:10]
            ]
            result["validation_count"] = exc.error_count()
        if isinstance(exc, BaseExceptionGroup):
            result["children"] = [
                describe(child, depth + 1) for child in exc.exceptions[:5]
            ]
            result["omitted_children"] = max(0, len(exc.exceptions) - 5)
        cause = exc.__cause__ or (
            exc.__context__ if not exc.__suppress_context__ else None
        )
        if cause is not None:
            result["cause"] = describe(cause, depth + 1)
        return result

    return describe(error, 0)


def log_investigation_event(
    event: str,
    *,
    task_id: str,
    generation: int | None = None,
    phase: str | None = None,
    search_round: int | None = None,
    error: BaseException | None = None,
    retryable: bool | None = None,
) -> None:
    """Emit one JSON message; diagnostics are observations, not durable status."""
    scope = _context.get()
    if scope is not None and scope.task_id != task_id:
        scope = None
    if scope is not None and event == "phase_committed":
        scope.phase, scope.search_round = phase, search_round
    payload = {
        "event": event,
        "timestamp": datetime.now(UTC).isoformat(),
        "task_id": task_id,
        "repository": scope.repository if scope else None,
        "issue_number": scope.issue_number if scope else None,
        "generation": generation
        if generation is not None
        else scope.generation
        if scope
        else None,
        "phase": phase if phase is not None else scope.phase if scope else None,
        "search_round": search_round
        if search_round is not None
        else scope.search_round
        if scope
        else None,
    }
    try:
        if error is not None:
            payload["error"] = _error_diagnostics(error)
        if retryable is not None:
            payload["retryable"] = retryable
        logger.log(
            logging.ERROR if error is not None else logging.INFO,
            "%s",
            json.dumps(payload, ensure_ascii=False),
        )
    except Exception:
        # Diagnostic formatting/sink failures must not replace business errors
        # or change committed task status. Never fall back to raw exception text.
        return
