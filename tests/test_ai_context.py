"""Tests for the AI context feature (Epic 07 / #116).

Three layers:

* the pluggable budget-aware summary service (the roll-up algorithm);
* the extended ``LlmContextCell`` kinds on the generation contract;
* the ``/notebooks/{id}/ai-context`` store/load endpoints (owner-scoped).
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import DEV_JWT_SECRET, Settings, settings
from app.modules.ai_context.services.summary import (
    MAX_CONTEXT_ITEMS,
    CompactOldestStrategy,
    LlmSummaryStrategy,
    build_summary_service,
)
from app.modules.llm.schemas.llm_schemas import GenerateRequest, LlmContextCell


def _cell(kind: str, source: str) -> LlmContextCell:
    return LlmContextCell(kind=kind, source=source)


def _ctx_bytes(items: list[LlmContextCell]) -> int:
    return sum(len(item.source.encode("utf-8")) for item in items)


# ─── Summary service — the budget-aware roll-up ───────────────────────────────


def test_summary_no_rollup_when_within_budget() -> None:
    items = [_cell("code", "a" * 10), _cell("markdown", "b" * 10)]
    result = CompactOldestStrategy().summarize(items, byte_cap=100)
    # Nothing to compact: context unchanged, no summary.
    assert result.summary == ""
    assert result.context == items


def test_summary_rolls_up_oldest_when_over_byte_budget() -> None:
    # 8 cells of ~46 bytes each (368 bytes) against a 100-byte budget.
    items = [_cell("code", f"line{i} " + "x" * 40) for i in range(8)]
    result = CompactOldestStrategy().summarize(items, byte_cap=100)

    assert result.summary != ""
    assert result.context[0].kind == "summary"
    # Invariant: the rolled-up context fits the byte cap and the slot ceiling.
    assert _ctx_bytes(result.context) <= 100
    assert len(result.context) <= MAX_CONTEXT_ITEMS
    # The newest cell is kept verbatim (nearest cells matter most).
    assert result.context[-1] == items[-1]


def test_summary_rolls_up_when_too_many_items_even_if_bytes_fit() -> None:
    items = [_cell("code", str(i)) for i in range(15)]
    result = CompactOldestStrategy().summarize(items, byte_cap=8192)

    assert len(result.context) <= MAX_CONTEXT_ITEMS
    assert result.context[0].kind == "summary"
    # 15 items → keep 9 newest verbatim + 1 summary of the 6 oldest.
    assert "6 earlier cell(s) summarised" in result.summary
    assert result.context[-1] == items[-1]


def test_summary_empty_input() -> None:
    result = CompactOldestStrategy().summarize([], byte_cap=100)
    assert result.context == []
    assert result.summary == ""


def test_max_context_items_single_source_of_truth() -> None:
    # The roll-up ceiling must equal GenerateRequest.context's max_length, so the
    # rolled-up context can never exceed what /llm/generate accepts. Behavioural
    # check (no metadata introspection): exactly the cap is fine, one more is 422.
    at_cap = [{"kind": "code", "source": "x"} for _ in range(MAX_CONTEXT_ITEMS)]
    GenerateRequest(prompt="p", context=at_cap)
    with pytest.raises(ValidationError):
        GenerateRequest(prompt="p", context=[*at_cap, {"kind": "code", "source": "x"}])


def test_summary_item_respects_per_item_source_cap() -> None:
    # Many tiny cells: the folded digest would exceed MAX_CONTEXT_SOURCE_LENGTH
    # (8000) while byte_cap is 8192. The summary item must be capped to the
    # per-item source limit so LlmContextCell(...) never raises a ValidationError
    # (which the service would surface as a 500). Covers direct/llm/future reuse.
    items = [_cell("code", "x") for _ in range(2000)]
    result = CompactOldestStrategy().summarize(items, byte_cap=8192)
    assert result.context[0].kind == "summary"
    assert len(result.context[0].source) <= 8000  # MAX_CONTEXT_SOURCE_LENGTH


def test_build_summary_service_default_and_switch() -> None:
    assert isinstance(build_summary_service(), CompactOldestStrategy)
    assert isinstance(build_summary_service("compact-oldest"), CompactOldestStrategy)
    assert isinstance(build_summary_service("llm"), LlmSummaryStrategy)


def test_build_summary_service_unknown_strategy_raises() -> None:
    with pytest.raises(ValueError, match="Unknown LLM_CONTEXT_SUMMARY_STRATEGY"):
        build_summary_service("does-not-exist")


class _FakeProvider:
    """A SummaryProvider stub for the LLM strategy (no real Bedrock)."""

    def __init__(self, text: str | None = None, error: Exception | None = None) -> None:
        self._text = text
        self._error = error
        self.calls: list[dict[str, object]] = []

    def converse(self, **kwargs: object):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        from app.modules.llm.services.bedrock_client import LlmProviderResponse

        return LlmProviderResponse(text=self._text or "", model="fake")


def test_llm_strategy_uses_model_summary_for_the_folded_cells() -> None:
    provider = _FakeProvider(text="x and y are arrays of orders")
    strategy = LlmSummaryStrategy(provider, model_id="fake-model", temperature=0.2)
    items = [_cell("code", f"const v{i} = {i}") for i in range(15)]

    result = strategy.summarize(items, byte_cap=8192)

    assert result.context[0].kind == "summary"
    assert "x and y are arrays of orders" in result.summary
    assert len(result.context) <= MAX_CONTEXT_ITEMS
    assert result.context[-1] == items[-1]  # newest kept verbatim
    assert len(provider.calls) == 1  # the folded prefix was summarised once


def test_llm_strategy_falls_back_to_deterministic_on_provider_failure() -> None:
    provider = _FakeProvider(error=RuntimeError("bedrock down"))
    strategy = LlmSummaryStrategy(provider, model_id="fake-model", temperature=0.2)
    items = [_cell("code", str(i)) for i in range(15)]

    result = strategy.summarize(items, byte_cap=8192)

    # Degrades to the deterministic digest — storing context never fails.
    assert "earlier cell(s) summarised" in result.summary
    assert _ctx_bytes(result.context) <= 8192


def test_llm_strategy_no_rollup_when_within_budget_skips_the_model() -> None:
    provider = _FakeProvider(text="unused")
    strategy = LlmSummaryStrategy(provider, model_id="fake-model", temperature=0.2)
    items = [_cell("code", "a" * 10)]

    result = strategy.summarize(items, byte_cap=8192)

    assert result.summary == ""
    assert result.context == items
    assert provider.calls == []  # no fold → no model call


# ─── Contract — extended context kinds ────────────────────────────────────────


def test_generate_accepts_extended_context_kinds() -> None:
    for kind in ["code", "markdown", "text", "output", "globals", "summary"]:
        assert _cell(kind, "x").kind == kind
    request = GenerateRequest(
        prompt="group rows",
        context=[
            {"kind": "globals", "source": "items: array<object>[100]"},
            {"kind": "output", "source": "[{category: 'a'}]"},
        ],
    )
    assert [c.kind for c in request.context] == ["globals", "output"]


def test_generate_rejects_unknown_context_kind() -> None:
    with pytest.raises(ValidationError):
        LlmContextCell(kind="image", source="x")


# ─── Settings validation ──────────────────────────────────────────────────────


def test_settings_reject_unknown_summary_strategy() -> None:
    # Operator contract: a typo must fail fast at startup, not on the first call.
    with pytest.raises(ValidationError, match="LLM_CONTEXT_SUMMARY_STRATEGY"):
        Settings(_env_file=None, llm_context_summary_strategy="does-not-exist")
    # Both valid ids are accepted.
    assert Settings(_env_file=None, llm_context_summary_strategy="llm")
    assert Settings(_env_file=None, llm_context_summary_strategy="compact-oldest")


def test_settings_defaults_present() -> None:
    cfg = Settings(_env_file=None)
    assert cfg.llm_context_summary_strategy == "compact-oldest"
    assert cfg.jwt_secret == DEV_JWT_SECRET  # sanity: still a dev config


# ─── Endpoints — /notebooks/{id}/ai-context ──────────────────────────────────


def _login(client: TestClient, email: str = "ctx-owner@example.com") -> dict[str, str]:
    otp = client.post(
        f"{settings.api_prefix}/auth/otp/request", json={"email": email}
    ).json()["otp"]
    body = client.post(
        f"{settings.api_prefix}/auth/otp/verify",
        json={"email": email, "otp": otp},
    ).json()
    return {"Authorization": f"Bearer {body['accessToken']}"}


def _create_notebook(client: TestClient, headers: dict[str, str]) -> str:
    notebook_id = str(uuid4())
    payload = {
        "id": notebook_id,
        "title": "Ctx notebook",
        "formatVersion": 1,
        "cells": [
            {
                "id": str(uuid4()),
                "kind": "code",
                "content": "console.log(1)",
                "updatedAt": 1779367200000,
            }
        ],
    }
    response = client.post(
        f"{settings.api_prefix}/notebooks", json=payload, headers=headers
    )
    assert response.status_code in (200, 201)
    return notebook_id


def _url(notebook_id: str) -> str:
    return f"{settings.api_prefix}/notebooks/{notebook_id}/ai-context"


def test_put_then_get_roundtrip(client: TestClient) -> None:
    headers = _login(client)
    notebook_id = _create_notebook(client, headers)

    body = {
        "context": [
            {"kind": "code", "source": "const x = 1"},
            {"kind": "markdown", "source": "# heading"},
        ]
    }
    put = client.put(_url(notebook_id), json=body, headers=headers)
    assert put.status_code == 200
    data = put.json()
    assert data["notebookId"] == notebook_id
    assert data["historyCount"] == 2
    # Within budget → context stored unchanged.
    assert [c["kind"] for c in data["context"]] == ["code", "markdown"]
    assert data["summary"] == ""
    assert data["updatedAt"] is not None

    got = client.get(_url(notebook_id), headers=headers)
    assert got.status_code == 200
    assert got.json()["context"] == data["context"]


def test_get_default_empty_when_never_stored(client: TestClient) -> None:
    headers = _login(client)
    notebook_id = _create_notebook(client, headers)

    got = client.get(_url(notebook_id), headers=headers)
    assert got.status_code == 200
    payload = got.json()
    assert payload["context"] == []
    assert payload["summary"] == ""
    assert payload["historyCount"] == 0
    assert payload["updatedAt"] is None


def test_ai_context_requires_auth(client: TestClient) -> None:
    got = client.get(_url(str(uuid4())))
    assert got.status_code == 401


def test_ai_context_notebook_not_found(client: TestClient) -> None:
    headers = _login(client)
    response = client.put(_url(str(uuid4())), json={"context": []}, headers=headers)
    assert response.status_code == 404


def test_ai_context_other_owner_forbidden(client: TestClient) -> None:
    owner = _login(client, "owner-a@example.com")
    notebook_id = _create_notebook(client, owner)

    other = _login(client, "owner-b@example.com")
    response = client.get(_url(notebook_id), headers=other)
    assert response.status_code == 403


def test_put_rejects_oversized_store_bytes(client: TestClient) -> None:
    headers = _login(client)
    notebook_id = _create_notebook(client, headers)
    # The stored-history byte cap is llm_max_prompt_bytes (8192).
    # 2 items × 4500 bytes = 9000 > 8192.
    body = {"context": [{"kind": "code", "source": "x" * 4500} for _ in range(2)]}
    response = client.put(_url(notebook_id), json=body, headers=headers)
    assert response.status_code == 422


def test_put_rejects_oversized_request_body(client: TestClient) -> None:
    headers = _login(client)
    notebook_id = _create_notebook(client, headers)
    # The raw HTTP body exceeds llm_max_total_bytes (16 KiB) — rejected by the
    # shared body-size guard before the model is parsed.
    body = {"context": [{"kind": "code", "source": "x" * 16_384}]}
    response = client.put(_url(notebook_id), json=body, headers=headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_too_large"


def test_put_rejects_oversized_content_length(client: TestClient) -> None:
    headers = _login(client)
    notebook_id = _create_notebook(client, headers)
    headers["Content-Length"] = str(settings.llm_max_total_bytes + 1)
    response = client.put(
        _url(notebook_id), json={"context": []}, headers=headers
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_too_large"


def test_put_rolls_up_context_over_budget(client: TestClient) -> None:
    headers = _login(client)
    notebook_id = _create_notebook(client, headers)
    # 20 small cells: under the byte cap but > 10 items, so the roll-up fires on
    # the item-count ceiling and folds the oldest into a summary item.
    body = {
        "context": [{"kind": "code", "source": f"const v{i} = {i}"} for i in range(20)]
    }
    response = client.put(_url(notebook_id), json=body, headers=headers)
    assert response.status_code == 200
    data = response.json()

    assert data["historyCount"] == 20
    assert len(data["context"]) <= MAX_CONTEXT_ITEMS
    assert data["context"][0]["kind"] == "summary"
    assert data["summary"] != ""
    # Stored context fits the generation byte budget, ready for /llm/generate.
    ctx_bytes = sum(len(c["source"].encode("utf-8")) for c in data["context"])
    assert ctx_bytes <= settings.llm_max_prompt_bytes


def test_delete_clears_stored_context(client: TestClient) -> None:
    headers = _login(client)
    notebook_id = _create_notebook(client, headers)
    client.put(
        _url(notebook_id),
        json={"context": [{"kind": "code", "source": "const x = 1"}]},
        headers=headers,
    )

    deleted = client.delete(_url(notebook_id), headers=headers)
    assert deleted.status_code == 204

    got = client.get(_url(notebook_id), headers=headers)
    assert got.json()["context"] == []
    assert got.json()["historyCount"] == 0
