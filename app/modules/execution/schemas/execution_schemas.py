"""Request/response DTOs for ``POST /api/v1/execute``.

The contract matches two frontend sources of truth:

* ``outputs`` mirror the ``cell.outputs`` shape — the UI runtime
  ``OutputItem[]`` (``ui/src/features/notebook/runtime/types.ts``) — so that
  ``OutputView`` renders a backend run identically to a local one;
* the top-level ``status`` matches the task acceptance criteria
  (``ok | error | timeout | unsupported_language``) and a subset of
  ``ExecutionResult`` from ``docs/execution-architecture.md`` §9.

``executedOn`` is always ``"backend"`` (the unified format, §5.3/§9.2).

Note: the subprocess runner behind this endpoint is a debug/fallback runner,
**not** a production sandbox (see ``docs/execution-architecture.md`` §12). It
only emits ``stdout``/``stderr``/``error`` items; ``result``/``html``/``image``
remain part of the shared ``cell.outputs`` contract but are not produced by the
current runner.

The source-code size cap is enforced by ``ExecutionService`` (the layer with
access to ``settings.execute_max_code_bytes``), not here — the request schema
only enforces shape (non-empty ``code``, positive ``timeoutMs``).
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


# ─── SerializedValue — mirror of the UI runtime SerializedValue union ─────────


class PrimitiveValue(BaseModel):
    """A JSON-primitive value (``string | number | boolean | null``).

    Caveat: the controller serializes with ``response_model_exclude_none`` to
    keep optional fields (``stats``, ``error.stack``) absent rather than
    ``null`` — matching the UI ``cell.outputs`` optional semantics. That would
    also drop a genuine ``value: null`` here, but the subprocess runner never
    emits ``result`` items, so this branch is unreachable today. A future
    QuickJS path that emits results must revisit this (e.g. drop the global
    exclude_none in favour of per-field handling).
    """

    kind: Literal["primitive"] = "primitive"
    value: str | int | float | bool | None


class UndefinedValue(BaseModel):
    """The JavaScript ``undefined`` value."""

    kind: Literal["undefined"] = "undefined"


class ArrayValue(BaseModel):
    """An array of serialized values."""

    kind: Literal["array"] = "array"
    items: list[SerializedValue]


class ObjectValue(BaseModel):
    """An object as an ordered list of ``[key, value]`` entries."""

    kind: Literal["object"] = "object"
    entries: list[tuple[str, SerializedValue]]


class TruncatedValue(BaseModel):
    """A value cut off because the structure exceeded the depth budget."""

    kind: Literal["truncated"] = "truncated"
    placeholder: str


class FunctionValue(BaseModel):
    """A function value, represented by its name only."""

    kind: Literal["function"] = "function"
    name: str


SerializedValue = Annotated[
    Union[
        PrimitiveValue,
        UndefinedValue,
        ArrayValue,
        ObjectValue,
        TruncatedValue,
        FunctionValue,
    ],
    Field(discriminator="kind"),
]


# ─── OutputItem — mirror of the UI runtime OutputItem union ───────────────────


class StdoutItem(BaseModel):
    """A chunk of ``console.log``/``console.info`` output."""

    type: Literal["stdout"] = "stdout"
    text: str


class StderrItem(BaseModel):
    """A chunk of ``console.error``/``console.warn`` output."""

    type: Literal["stderr"] = "stderr"
    text: str


class ResultItem(BaseModel):
    """The serialized value of the last evaluated expression."""

    type: Literal["result"] = "result"
    value: SerializedValue


class ErrorItem(BaseModel):
    """A thrown error surfaced to the output panel."""

    type: Literal["error"] = "error"
    name: str
    message: str
    stack: str | None = None


class HtmlItem(BaseModel):
    """HTML rendered inside a sandboxed iframe (``display({type:'html'})``)."""

    type: Literal["html"] = "html"
    html: str


class ImageItem(BaseModel):
    """A base64-encoded image with its MIME type."""

    type: Literal["image"] = "image"
    mime: str
    data: str


OutputItem = Annotated[
    Union[
        StdoutItem,
        StderrItem,
        ResultItem,
        ErrorItem,
        HtmlItem,
        ImageItem,
    ],
    Field(discriminator="type"),
]


# Resolve the recursive forward references (SerializedValue nests itself).
ArrayValue.model_rebuild()
ObjectValue.model_rebuild()
ResultItem.model_rebuild()


# ─── Request / response envelope ──────────────────────────────────────────────


class ExecuteRequest(BaseModel):
    """Request body for ``POST /api/v1/execute``."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    code: str = Field(..., min_length=1)
    # Free-form on purpose: a non-JavaScript language must yield a 200 response
    # with status ``unsupported_language`` (acceptance criteria), not a 422.
    language: str = Field(default="javascript", min_length=1, max_length=64)
    # ``timeoutMs`` is optional; the server falls back to the configured default
    # and clamps to the configured maximum. Non-positive values are rejected.
    timeout_ms: int | None = Field(default=None, ge=1)

    def code_byte_length(self) -> int:
        """Return the UTF-8 byte length of ``code``."""
        return len(self.code.encode("utf-8"))


class ExecutionStats(BaseModel):
    """Execution metrics for diagnostics (``ExecutionResult.stats`` subset)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    duration_ms: int = Field(..., ge=0)
    memory_kb: int | None = Field(default=None, ge=0)


class ExecuteResponse(BaseModel):
    """Unified execution result (``cell.outputs``-compatible payload)."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    status: Literal["ok", "error", "timeout", "unsupported_language"]
    executed_on: Literal["backend"] = "backend"
    outputs: list[OutputItem] = Field(default_factory=list)
    stats: ExecutionStats | None = None
