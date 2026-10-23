"""Register syntax analyzers independently of the chunking policy."""

from collections.abc import Callable

from maintainer_lens.schemas.chunk import CodeChunk
from maintainer_lens.schemas.repository import RepositorySource
from maintainer_lens.services.go_structure import analyze_go_source
from maintainer_lens.services.java_structure import analyze_java_source
from maintainer_lens.services.python_chunker import analyze_python_source
from maintainer_lens.services.tree_sitter_structure import (
    analyze_js_ts_source,
)

SyntaxAnalyzer = Callable[[RepositorySource], list[CodeChunk]]


def _analyze_python(source: RepositorySource) -> list[CodeChunk]:
    """Normalize Python analysis to a single list of syntax structures."""

    top_level, methods = analyze_python_source(source)
    return [*top_level, *methods]


SYNTAX_ANALYZERS: dict[str, SyntaxAnalyzer] = {
    "python": _analyze_python,
    "javascript": analyze_js_ts_source,
    "typescript": analyze_js_ts_source,
    "go": analyze_go_source,
    "java": analyze_java_source,
}


def analyze_source_syntax(
    source: RepositorySource,
    language: str,
) -> list[CodeChunk]:
    """Use a registered analyzer or return no syntax hints."""

    analyzer = SYNTAX_ANALYZERS.get(language)

    if analyzer is None:
        return []

    return analyzer(source)
