"""Extraction of runnable code from noisy LLM output."""

import re

FENCED_CODE_RE = re.compile(
    r"```(?:[a-zA-Z0-9_+.-]+)?\s*(?P<code>.*?)```",
    re.DOTALL,
)


def extract_code(raw: str) -> str:
    """Return the best code candidate from a raw LLM response.

    Policy for several fenced blocks: choose the longest non-empty block.
    This avoids returning a tiny example when the model includes several
    snippets and keeps the behavior deterministic.
    """
    if not raw or not raw.strip():
        return ""

    blocks = [match.group("code").strip() for match in FENCED_CODE_RE.finditer(raw)]
    non_empty_blocks = [block for block in blocks if block]
    if non_empty_blocks:
        return max(non_empty_blocks, key=len)

    return raw.strip()
