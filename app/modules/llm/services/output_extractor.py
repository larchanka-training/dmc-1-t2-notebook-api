"""Extraction of runnable code from noisy LLM output."""

import re

FENCED_CODE_RE = re.compile(
    r"```(?:[a-zA-Z0-9_+.-]+)?\s*(?P<code>.*?)```",
    re.DOTALL,
)


def extract_code(raw: str) -> str:
    """Return the best code candidate from a raw LLM response.

    Policy
    ------
    * If the response contains one or more fenced code blocks, return the
      **longest non-empty** block. Avoids returning a tiny example when
      the model includes several snippets.

    * If the response contains **no fenced blocks**, return the trimmed
      raw text.

    The fenceless fallback is intentional and is not a contradiction
    with the generation system prompt
    (``_generation_system_prompt`` in ``generation_service.py``), which
    explicitly instructs the model to *return only executable code, no
    fences, no prose*. When the model obeys, there is no fence to find,
    so we must accept the whole response as code. Misbehaving responses
    that smuggle prose alongside the code without fences are caught one
    step later by :class:`EsbuildSyntaxValidator`, which fails the
    transform and triggers the repair loop.
    """
    if not raw or not raw.strip():
        return ""

    blocks = [match.group("code").strip() for match in FENCED_CODE_RE.finditer(raw)]
    non_empty_blocks = [block for block in blocks if block]
    if non_empty_blocks:
        return max(non_empty_blocks, key=len)

    return raw.strip()
