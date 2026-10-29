"""Offline checkpoint identity, model registration and migration contracts."""

import ast
import importlib.util
from io import StringIO
from pathlib import Path
from uuid import UUID

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from pydantic import ValidationError

import maintainer_lens.db.models as models
from maintainer_lens.schemas.investigation_checkpoint import (
    FrozenInvestigationRun,
    GraphCheckpointConfig,
    endpoint_fingerprint,
    execution_fingerprint,
)
from maintainer_lens.schemas.investigation_contracts import InvestigationInput


def _configuration(**changes):
    return GraphCheckpointConfig.model_validate(
        {
            "response_language": "en",
            "max_search_rounds": 3,
            "timeout_seconds": 120.0,
            "search_strategy": "tool",
            "tool_timeout_seconds": 60.0,
            "source_enabled": False,
            "history_top_k": 5,
            "source_top_k": 5,
            "embedding_provider": "test",
            "embedding_model": "embedding",
            "embedding_dimensions": 3,
            "embedding_endpoint_fingerprint": endpoint_fingerprint(
                "https://embedding.example/v1"
            ),
            "llm_model": "analysis",
            "llm_endpoint_fingerprint": endpoint_fingerprint("https://llm.example/v1"),
            "llm_max_output_tokens": 4096,
            "thinking_enabled": False,
            **changes,
        }
    )


def _request(**changes):
    return InvestigationInput(
        repository="checkpoint-tests/repository",
        issue_number=123,
        issue_title="Cleanup",
        issue_body=None,
        response_language="en",
        **changes,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_search_rounds", True),
        ("max_search_rounds", 4),
        ("timeout_seconds", True),
        ("timeout_seconds", 0),
        ("timeout_seconds", float("inf")),
        ("timeout_seconds", float("nan")),
        ("embedding_dimensions", False),
        ("history_top_k", 0),
        ("llm_max_output_tokens", -1),
        ("tool_timeout_seconds", False),
        ("tool_timeout_seconds", None),
        ("llm_model", " "),
        ("storage_version", 2),
        ("llm_endpoint_fingerprint", "raw-url"),
    ],
)
def test_invalid_configuration_has_no_external_dependencies(field, value):
    with pytest.raises(ValidationError):
        _configuration(**{field: value})


def test_strategy_configuration_is_consistent():
    with pytest.raises(ValidationError):
        _configuration(max_search_rounds=1)
    with pytest.raises(ValidationError):
        _configuration(search_strategy="deterministic")
    assert _configuration(search_strategy="deterministic", tool_timeout_seconds=None)


def test_frozen_run_round_trips_uuid_and_rejects_modified_input():
    request = _request(source_index_id=UUID("12345678-1234-4234-8234-123456789abc"))
    config = _configuration(source_enabled=True)
    run = FrozenInvestigationRun(
        task_id="task",
        request=request,
        execution=config,
        fingerprint=execution_fingerprint(request, config),
    )
    assert (
        FrozenInvestigationRun.model_validate_json(run.model_dump_json(), strict=True)
        == run
    )
    payload = run.model_dump()
    payload["request"]["issue_title"] = "Changed"
    with pytest.raises(ValidationError, match="fingerprint"):
        FrozenInvestigationRun.model_validate(payload)
    with pytest.raises(ValidationError):
        run.task_id = "other"


def test_frozen_language_source_and_non_secret_endpoint_identity():
    request = _request()
    for config in (
        _configuration(response_language="zh"),
        _configuration(source_enabled=True),
    ):
        with pytest.raises(ValidationError):
            FrozenInvestigationRun(
                task_id="task",
                request=request,
                execution=config,
                fingerprint=execution_fingerprint(request, config),
            )
    secret_url = "https://user:secret@llm.example/v1"
    digest = endpoint_fingerprint(secret_url)
    assert len(digest) == 64 and "secret" not in digest
    assert endpoint_fingerprint(secret_url + "/") == digest
    assert endpoint_fingerprint("https://other.example/v1") != digest
    with pytest.raises(ValidationError):
        _configuration(llm_api_key="forbidden")
    with pytest.raises(ValidationError):
        InvestigationInput.model_validate({**request.model_dump(), "search_round": 2})


def test_registered_tables_reuse_task_identity_and_cascade_without_new_status():
    for name, table_name in (
        ("InvestigationGraphRun", "investigation_graph_runs"),
        ("InvestigationCheckpoint", "investigation_checkpoints"),
        ("InvestigationCheckpointWrite", "investigation_checkpoint_writes"),
    ):
        model = getattr(models, name)
        assert name in models.__all__
        table = models.Base.metadata.tables[table_name]
        assert (
            table is model.__table__
            and "status" not in table.c
            and "claim_token" not in table.c
        )
        foreign_key = next(iter(table.c.task_id.foreign_keys))
        assert foreign_key.ondelete == "CASCADE"
        assert foreign_key.target_fullname == (
            "investigation_tasks.id"
            if name == "InvestigationGraphRun"
            else "investigation_graph_runs.task_id"
        )
    # Node output callbacks are allowed to finish before the snapshot callback.
    writes = models.InvestigationCheckpointWrite.__table__
    assert not writes.c.checkpoint_id.foreign_keys
    assert list(writes.primary_key.columns.keys()) == [
        "task_id",
        "checkpoint_ns",
        "checkpoint_id",
        "node_task_id",
        "write_index",
    ]


def test_checkpoint_migration_is_the_single_connected_head():
    directory = Path(__file__).resolve().parents[1] / "migrations" / "versions"
    revisions = {}
    for path in directory.glob("*.py"):
        values = {}
        for node in ast.parse(path.read_text()).body:
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id in ("revision", "down_revision")
            ):
                values[node.target.id] = ast.literal_eval(node.value)
        if "revision" in values:
            revisions[values["revision"]] = values["down_revision"]
    assert revisions["f9a4d6e8b205"] == "e8b3c7d9a204"
    assert set(revisions) - set(revisions.values()) == {"f9a4d6e8b205"}
    revision = "f9a4d6e8b205"
    seen = set()
    while revision is not None:
        assert revision not in seen
        seen.add(revision)
        revision = revisions[revision]
    assert seen == set(revisions)


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_checkpoint_migration_renders_offline_without_touching_task_rows(direction):
    path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "f9a4d6e8b205_add_investigation_checkpoints.py"
    )
    spec = importlib.util.spec_from_file_location("checkpoint_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    migration.op = Operations(context)
    getattr(migration, direction)()
    sql = output.getvalue()
    assert "ALTER TABLE investigation_tasks" not in sql
    assert "UPDATE " not in sql and "INSERT INTO " not in sql
    tables = [
        "investigation_graph_runs",
        "investigation_checkpoints",
        "investigation_checkpoint_writes",
    ]
    ordered = tables if direction == "upgrade" else list(reversed(tables))
    command = "CREATE TABLE" if direction == "upgrade" else "DROP TABLE"
    assert [sql.index(f"{command} {name}") for name in ordered] == sorted(
        sql.index(f"{command} {name}") for name in ordered
    )
    if direction == "upgrade":
        assert "payload BYTEA" in sql and "execution_payload JSONB" in sql
        assert "REFERENCES investigation_tasks (id) ON DELETE CASCADE" in sql
        assert (
            sql.count("REFERENCES investigation_graph_runs (task_id) ON DELETE CASCADE")
            == 2
        )
