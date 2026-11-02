"""Provider transport contract tests; no external requests or real model."""
import json
from unittest.mock import patch

import pytest

from vl_scenario_async.openai_compatible import OpenAICompatibleProvider, ProviderError


class FakeResponse:
    status = 200
    def __init__(self, value):
        self.value = json.dumps(value).encode()
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self, maximum): return self.value[:maximum]


class Opener:
    def __init__(self, response): self.response, self.requests = response, []
    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return FakeResponse(self.response)


def job(*, model_alias="vision", revision="revision-1", schema=None):
    return {"job_id": "1" * 64, "identity": {"model_alias": model_alias, "model_revision": revision},
            "payload": {"text": "Describe the synthetic image.", "parameters": {}, "response_schema": schema}}


def provider(**kwargs):
    return OpenAICompatibleProvider(endpoint="http://127.0.0.1:1234/v1/chat/completions", model_id="model-a",
        model_alias="vision", model_revision="revision-1", api_key="example-private-token", allow_loopback_http=True, **kwargs)


def response(content, *, model="model-a", finish="stop"):
    return {"model": model, "choices": [{"finish_reason": finish, "message": {"content": content}}]}


def test_text_and_synthetic_image_are_bound_to_host_selected_model():
    opener = Opener(response("red square"))
    with patch("urllib.request.build_opener", return_value=opener):
        result = provider()(job(), b"\x89PNG\r\n\x1a\nsynthetic")
    assert result == {"text": "red square", "structured": None}
    request, timeout = opener.requests[0]
    assert request.headers["Authorization"] == "Bearer example-private-token"
    assert request.headers["Idempotency-key"] == "1" * 64
    body = json.loads(request.data)
    assert body["model"] == "model-a" and body["max_tokens"] == 128
    assert body["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "example-private-token" not in repr(provider())


def test_wrong_model_identity_or_untrusted_parameters_fail_before_network():
    with patch("urllib.request.build_opener") as opened:
        with pytest.raises(ProviderError): provider()(job(model_alias="other"), None)
        j = job(); j["payload"]["parameters"] = {"model": "attacker-choice"}
        with pytest.raises(ProviderError): provider()(j, None)
        opened.assert_not_called()


@pytest.mark.parametrize("endpoint", ["http://example.org/v1/chat/completions", "https://user:pass@example.org/v1/chat/completions", "https://example.org/v1/chat/completions?api_key=secret"])
def test_endpoints_do_not_allow_insecure_remote_or_embedded_credentials(endpoint):
    with pytest.raises(ProviderError):
        OpenAICompatibleProvider(endpoint, "m", "a", "v", allow_loopback_http=True)


def test_structured_response_is_validated_and_incomplete_response_is_rejected():
    schema = {"type": "object", "required": ["color"], "properties": {"color": {"const": "red"}}, "additionalProperties": False}
    opener = Opener(response('{"color":"red"}'))
    with patch("urllib.request.build_opener", return_value=opener):
        assert provider(request_json_mode=True)(job(schema=schema), None) == {"text": None, "structured": {"color": "red"}}
    assert json.loads(opener.requests[0][0].data)["response_format"] == {"type": "json_object"}
    with patch("urllib.request.build_opener", return_value=Opener(response('{"color":"blue"}'))):
        with pytest.raises(ProviderError): provider()(job(schema=schema), None)
    with patch("urllib.request.build_opener", return_value=Opener(response("partial", finish="length"))):
        with pytest.raises(ProviderError): provider()(job(), None)
    with patch("urllib.request.build_opener", return_value=Opener({"choices": [{"finish_reason": "stop", "message": {"content": "x"}}]})):
        with pytest.raises(ProviderError): provider()(job(), None)
