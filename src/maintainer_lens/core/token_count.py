"""Count source text tokens with the configured baseline tokenizer."""

from functools import lru_cache

import tiktoken


@lru_cache(maxsize=1)
def _get_encoding() -> tiktoken.Encoding:
    """Load the baseline encoding once per Python process."""

    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """Count tokens, treating special-token-looking text literally."""

    return len(
        _get_encoding().encode(
            text,
            disallowed_special=(),
        )
    )
