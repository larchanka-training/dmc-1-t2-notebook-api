from unittest.mock import MagicMock, patch

import pytest

from app.modules.llm.services.bedrock_client import (
    BedrockClient,
    clear_bedrock_client_cache,
    parse_guard_json,
)
from app.modules.llm.services.errors import LlmProviderError, LlmServiceError


def _require_boto3() -> None:
    pytest.importorskip("boto3")


def _botocore_exceptions():
    return pytest.importorskip("botocore.exceptions")


def setup_function() -> None:
    clear_bedrock_client_cache()


def teardown_function() -> None:
    clear_bedrock_client_cache()


def _client_error(code: str) -> Exception:
    return _botocore_exceptions().ClientError(
        error_response={"Error": {"Code": code, "Message": "provider error"}},
        operation_name="Converse",
    )


def _converse(client: BedrockClient, model_id: str = "eu.amazon.nova-lite-v1:0"):
    return client.converse(
        model_id=model_id,
        system_prompt="system",
        user_prompt="prompt",
        max_tokens=100,
        temperature=0.0,
    )


def test_bedrock_client_parses_normal_response() -> None:
    _require_boto3()
    fake_client = MagicMock()
    fake_client.converse.return_value = {
        "output": {"message": {"content": [{"text": "hello"}]}},
        "usage": {"inputTokens": 10, "outputTokens": 5},
    }

    with patch(
        "app.modules.llm.services.bedrock_client.boto3.client",
        return_value=fake_client,
    ):
        bedrock = BedrockClient(region_name="eu-north-1", timeout_seconds=30)
        result = _converse(bedrock)

    assert result.text == "hello"
    assert result.model == "eu.amazon.nova-lite-v1:0"
    assert result.prompt_tokens == 10
    assert result.completion_tokens == 5


def test_bedrock_client_reuses_cached_client_and_sets_timeouts() -> None:
    _require_boto3()
    fake_client = MagicMock()
    fake_client.converse.return_value = {
        "output": {"message": {"content": [{"text": "hello"}]}},
    }

    with patch(
        "app.modules.llm.services.bedrock_client.boto3.client",
        return_value=fake_client,
    ) as boto_client:
        bedrock = BedrockClient(region_name="eu-north-1", timeout_seconds=30)
        _converse(bedrock)
        _converse(bedrock)

    assert boto_client.call_count == 1
    config = boto_client.call_args.kwargs["config"]
    assert config.connect_timeout == 5
    assert config.read_timeout == 30
    assert config.retries["max_attempts"] == 1


@pytest.mark.parametrize(
    ("provider_code", "expected_code", "expected_status"),
    [
        ("AccessDeniedException", "llm_access_denied", 500),
        ("ValidationException", "llm_internal", 500),
        ("ThrottlingException", "llm_throttled", 429),
    ],
)
def test_bedrock_client_maps_client_errors(
    provider_code: str,
    expected_code: str,
    expected_status: int,
) -> None:
    _require_boto3()
    fake_client = MagicMock()
    fake_client.converse.side_effect = _client_error(provider_code)

    with patch(
        "app.modules.llm.services.bedrock_client.boto3.client",
        return_value=fake_client,
    ):
        bedrock = BedrockClient(region_name="eu-north-1", timeout_seconds=30)
        with pytest.raises(LlmServiceError) as error:
            _converse(bedrock)

    assert error.value.code == expected_code
    assert error.value.status_code == expected_status


def test_bedrock_client_maps_endpoint_connection_error() -> None:
    _require_boto3()
    fake_client = MagicMock()
    fake_client.converse.side_effect = _botocore_exceptions().EndpointConnectionError(
        endpoint_url="https://bedrock-runtime.eu-north-1.amazonaws.com"
    )

    with patch(
        "app.modules.llm.services.bedrock_client.boto3.client",
        return_value=fake_client,
    ):
        bedrock = BedrockClient(region_name="eu-north-1", timeout_seconds=30)
        with pytest.raises(LlmServiceError) as error:
            _converse(bedrock)

    assert error.value.code == "llm_unavailable"
    assert error.value.status_code == 503


def test_parse_guard_json_rejects_invalid_shape() -> None:
    with pytest.raises(LlmProviderError):
        parse_guard_json('{"not_safe": true}')


def test_parse_guard_json_accepts_safe_true() -> None:
    assert parse_guard_json('{"safe": true}') is True
