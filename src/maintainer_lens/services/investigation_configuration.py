"""One non-secret execution profile shared by Worker assembly and URL reuse."""

from maintainer_lens.agents.investigation_analysis import (
    DEFAULT_ANALYSIS_MAX_OUTPUT_TOKENS,
    ResponseLanguage,
)
from maintainer_lens.core.config import Settings
from maintainer_lens.schemas.investigation_checkpoint import (
    GraphCheckpointConfig,
    endpoint_fingerprint,
)

WORKER_RESPONSE_LANGUAGE: ResponseLanguage = "zh"
WORKER_MAX_SEARCH_ROUNDS = 3
WORKER_GRAPH_TIMEOUT_SECONDS = 120.0
WORKER_TOOL_TIMEOUT_SECONDS = 60.0
WORKER_EMBEDDING_PROVIDER = "openai-compatible"


def make_worker_execution_config(
    settings: Settings, *, source_enabled: bool
) -> GraphCheckpointConfig:
    return GraphCheckpointConfig(
        response_language=WORKER_RESPONSE_LANGUAGE,
        max_search_rounds=WORKER_MAX_SEARCH_ROUNDS,
        timeout_seconds=WORKER_GRAPH_TIMEOUT_SECONDS,
        search_strategy="tool",
        source_enabled=source_enabled,
        history_top_k=5,
        source_top_k=5,
        embedding_provider=WORKER_EMBEDDING_PROVIDER,
        embedding_model=settings.embedding_model or "",
        embedding_dimensions=settings.embedding_dimensions,
        embedding_endpoint_fingerprint=endpoint_fingerprint(
            settings.embedding_base_url or ""
        ),
        llm_model=settings.llm_model_name or "",
        llm_endpoint_fingerprint=endpoint_fingerprint(settings.llm_base_url or ""),
        llm_max_output_tokens=getattr(
            settings, "llm_max_output_tokens", DEFAULT_ANALYSIS_MAX_OUTPUT_TOKENS
        ),
        thinking_enabled=False,
        tool_timeout_seconds=WORKER_TOOL_TIMEOUT_SECONDS,
    )
