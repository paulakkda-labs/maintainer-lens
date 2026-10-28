"""Encode, decode and persist versioned investigation result artifacts."""

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from maintainer_lens.agents.investigation.state import InvalidInvestigationResultError
from maintainer_lens.db.models.investigation import InvestigationTask
from maintainer_lens.db.models.investigation_result import (
    INVESTIGATION_RESULT_SCHEMA_VERSION,
    InvestigationResultRecord,
)
from maintainer_lens.schemas.investigation_contracts import InvestigationResult


class StoredInvestigationResultError(ValueError):
    """A stored artifact has an unsupported format or invalid task identity."""


def decode_investigation_result(
    record: InvestigationResultRecord,
    *,
    task_id: str,
    repository: str,
    issue_number: int,
) -> InvestigationResult:
    """Decode version 1 JSON without executing investigation dependencies.

    Structural and task-identity validation detect storage contract violations;
    execution-time citation/routing checks remain in the application entry.
    The caller must first establish that the task succeeded.
    """
    if (
        type(record.schema_version) is not int
        or record.schema_version != INVESTIGATION_RESULT_SCHEMA_VERSION
    ):
        raise StoredInvestigationResultError("Unsupported result schema version")

    if record.task_id != task_id:
        raise StoredInvestigationResultError("Stored result belongs to another task")

    try:
        # Strict JSON validation accepts serialized UUIDs without coercing
        # numeric/boolean fields, and rejects non-finite numeric evidence.
        payload = json.dumps(record.result_payload, allow_nan=False)
        result = InvestigationResult.model_validate_json(payload, strict=True)
    except (TypeError, ValueError) as exc:
        raise StoredInvestigationResultError("Invalid stored result payload") from exc

    if (result.repository, result.issue_number) != (repository, issue_number):
        raise StoredInvestigationResultError("Stored result does not match its task")

    return result


def _serialize_result(result: InvestigationResult) -> dict[str, Any]:
    """Snapshot structural data before the first await, rejecting non-JSON values."""
    if not isinstance(result, InvestigationResult):
        raise InvalidInvestigationResultError("Expected InvestigationResult")

    if (
        result.model_extra
        or not vars(result).keys() <= InvestigationResult.model_fields.keys()
    ):
        raise InvalidInvestigationResultError("Unexpected investigation result fields")

    try:
        # Revalidate plain data, including nested models altered after construction.
        validated = InvestigationResult.model_validate(result.model_dump(), strict=True)
        payload = validated.model_dump(mode="json")
        json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise InvalidInvestigationResultError("Result is not valid JSON data") from exc

    return payload


async def upsert_investigation_result(
    session: AsyncSession,
    *,
    task_id: str,
    claim_token: str,
    result: InvestigationResult,
) -> bool:
    """Write a result under a valid claim; the caller must commit or roll back.

    True means the upsert executed, not that its transaction committed. False
    means the task is missing or no longer has the supplied active claim.
    Execution-time citation/routing validation belongs to the application entry.
    """
    payload = _serialize_result(result)

    locked_task = (
        await session.execute(
            select(InvestigationTask.repository, InvestigationTask.issue_number)
            .where(InvestigationTask.id == task_id)
            .with_for_update()
        )
    ).one_or_none()
    if locked_task is None:
        return False

    # Evaluate the database clock after acquiring the lock: waiting for another
    # transaction must not preserve an ownership check made before lease expiry.
    owned_task_id = await session.scalar(
        select(InvestigationTask.id).where(
            InvestigationTask.id == task_id,
            InvestigationTask.status == "running",
            InvestigationTask.claim_token == claim_token,
            InvestigationTask.lease_expires_at > func.clock_timestamp(),
        )
    )
    if owned_task_id is None:
        return False

    if tuple(locked_task) != (payload["repository"], payload["issue_number"]):
        raise InvalidInvestigationResultError("Result does not match its task identity")

    statement = insert(InvestigationResultRecord).values(
        task_id=task_id,
        schema_version=INVESTIGATION_RESULT_SCHEMA_VERSION,
        result_payload=payload,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[InvestigationResultRecord.task_id],
        set_={
            "schema_version": statement.excluded.schema_version,
            "result_payload": statement.excluded.result_payload,
            "updated_at": func.clock_timestamp(),
        },
    )
    await session.execute(statement)
    return True
