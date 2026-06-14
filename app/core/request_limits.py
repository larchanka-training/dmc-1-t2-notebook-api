"""Shared HTTP request-body size guard (defence at the input boundary).

Rejects an oversized body **before** it is buffered/parsed into a model, so a
hostile client cannot make the server spend memory/CPU on multi-megabyte JSON
that business validation would reject anyway. Reused by the LLM and AI-context
endpoints.
"""

from fastapi import HTTPException, Request, status


def _too_large(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"code": "request_too_large", "message": message},
    )


async def enforce_body_size(
    request: Request, *, max_bytes: int, error_message: str
) -> None:
    """Reject an HTTP body larger than ``max_bytes``.

    Two-stage enforcement:

    1. Short-circuit on the ``Content-Length`` header **before** buffering the
       body (cheap; treated as a defensive hint — a malformed/absent header
       falls through).
    2. Buffer and re-check the actual byte length.
    """
    content_length_header = request.headers.get("content-length")
    if content_length_header is not None:
        try:
            declared = int(content_length_header)
        except ValueError:
            declared = -1  # malformed header — fall through to the buffered check
        if declared > max_bytes:
            raise _too_large(error_message)

    body = await request.body()
    if len(body) > max_bytes:
        raise _too_large(error_message)
