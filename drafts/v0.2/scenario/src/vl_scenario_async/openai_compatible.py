"""Host-configured, bounded OpenAI-compatible MLLM adapter.

The scenario cannot supply endpoints, credentials or a different model. This
module sends images only when the trusted host invokes it. It does not log
prompts, images, keys or raw provider errors. It is not a sandbox or an egress
policy implementation; deployments must independently authorize data transfer.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import ipaddress
import json
import math
import ssl
import urllib.error
import urllib.parse
import urllib.request

from jsonschema import Draft202012Validator


class ProviderError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderError("Provider redirects are not permitted")


def _unique(pairs):
    output = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate JSON key")
        output[key] = value
    return output


def _bad_constant(_):
    raise ValueError("nonfinite JSON constant")


def _self_contained(value):
    if isinstance(value, dict):
        if {"$ref", "$dynamicRef", "$id"} & set(value):
            raise ProviderError("Response schema must not contain references or IDs")
        for entry in value.values():
            _self_contained(entry)
    elif isinstance(value, list):
        for entry in value:
            _self_contained(entry)


@dataclass(frozen=True)
class OpenAICompatibleProvider:
    """Minimal one-request adapter; HTTP is allowed only to explicit loopback."""
    endpoint: str
    model_id: str
    model_alias: str
    model_revision: str
    api_key: str | None = field(default=None, repr=False)
    timeout: float = 60.0
    max_tokens: int = 128
    max_response_bytes: int = 262144
    allow_loopback_http: bool = False
    disable_thinking: bool = False
    request_json_mode: bool = False

    def __post_init__(self):
        parts = urllib.parse.urlsplit(self.endpoint)
        if (parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or not parts.path.endswith("/chat/completions")):
            raise ProviderError("A configured chat-completions endpoint without credentials or query is required")
        if parts.scheme == "http":
            try:
                loopback = ipaddress.ip_address(parts.hostname).is_loopback
            except ValueError:
                loopback = parts.hostname == "localhost"
            if not self.allow_loopback_http or not loopback:
                raise ProviderError("HTTP is allowed only for explicitly configured loopback testing or a trusted tunnel")
        for name in ("model_id", "model_alias", "model_revision"):
            value = getattr(self, name)
            if type(value) is not str or not 1 <= len(value) <= 512:
                raise ProviderError("Configured model identity is invalid")
        if self.api_key is not None and (type(self.api_key) is not str or "\n" in self.api_key or "\r" in self.api_key):
            raise ProviderError("Invalid credential configuration")
        if type(self.max_tokens) is not int or not 1 <= self.max_tokens <= 4096:
            raise ProviderError("Invalid configured token limit")
        if type(self.max_response_bytes) is not int or not 1024 <= self.max_response_bytes <= 1048576:
            raise ProviderError("Invalid configured response limit")
        if type(self.timeout) not in (int, float) or not math.isfinite(self.timeout) or not 0 < self.timeout <= 300:
            raise ProviderError("Invalid configured timeout")

    def __call__(self, job, image):
        if type(job) is not dict or type(job.get("identity")) is not dict or type(job.get("payload")) is not dict:
            raise ProviderError("Invalid authorized job")
        identity, payload = job["identity"], job["payload"]
        if identity.get("model_alias") != self.model_alias or identity.get("model_revision") != self.model_revision:
            raise ProviderError("Job model does not match the configured provider binding")
        if set(payload) != {"text", "response_schema", "parameters"} or type(payload["parameters"]) is not dict:
            raise ProviderError("Invalid job payload")
        text = payload["text"]
        if text is not None and (type(text) is not str or len(text.encode("utf-8", "strict")) > 65536):
            raise ProviderError("Invalid prompt")
        schema = payload["response_schema"]
        if schema is not None:
            if type(schema) is not dict:
                raise ProviderError("Invalid response schema")
            _self_contained(schema)
            try:
                Draft202012Validator.check_schema(schema)
            except Exception as exc:
                raise ProviderError("Invalid response schema") from exc
        if set(payload["parameters"]) - {"max_tokens", "temperature"}:
            raise ProviderError("Unsupported model parameters")
        limit = payload["parameters"].get("max_tokens", self.max_tokens)
        temperature = payload["parameters"].get("temperature", 0)
        if type(limit) is not int or not 1 <= limit <= self.max_tokens:
            raise ProviderError("Requested token limit is not authorized")
        if type(temperature) not in (int, float) or not math.isfinite(temperature) or not 0 <= temperature <= 2:
            raise ProviderError("Invalid temperature")
        content = []
        if text:
            content.append({"type": "text", "text": text})
        if image is not None:
            if type(image) is not bytes or len(image) > 10 * 1024 * 1024:
                raise ProviderError("Invalid image size or type")
            if image.startswith(b"\x89PNG\r\n\x1a\n"):
                mime = "image/png"
            elif image.startswith(b"\xff\xd8\xff"):
                mime = "image/jpeg"
            else:
                raise ProviderError("Only PNG and JPEG images are accepted")
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}"}})
        if not content:
            raise ProviderError("A text or image input is required")
        body_data = {"model": self.model_id, "messages": [{"role": "user", "content": content}],
                     "max_tokens": limit, "temperature": temperature}
        if self.disable_thinking:
            body_data["chat_template_kwargs"] = {"enable_thinking": False}
        if schema is not None and self.request_json_mode:
            # Host opts in only for a compatible server; local schema
            # validation is still mandatory and does not rely on this hint.
            body_data["response_format"] = {"type": "json_object"}
        body = json.dumps(body_data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        job_id = job.get("job_id")
        if job_id is not None:
            if type(job_id) is not str or len(job_id) != 64 or any(c not in "0123456789abcdef" for c in job_id):
                raise ProviderError("Invalid idempotency key")
            # A provider may ignore this header; remote exactly-once is not guaranteed.
            headers["Idempotency-Key"] = job_id
        request = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
        handlers = [urllib.request.ProxyHandler({}), _NoRedirect()]
        if self.endpoint.startswith("https://"):
            handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        try:
            with urllib.request.build_opener(*handlers).open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise ProviderError("Provider denied or failed the request")
                raw = response.read(self.max_response_bytes + 1)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            raise ProviderError("Provider is unavailable or rejected the request") from exc
        if len(raw) > self.max_response_bytes:
            raise ProviderError("Provider response exceeded its size limit")
        try:
            data = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_unique, parse_constant=_bad_constant)
            if type(data) is not dict or data.get("model") != self.model_id:
                raise ValueError("model mismatch")
            choices = data["choices"]
            if type(choices) is not list or len(choices) != 1 or choices[0].get("finish_reason") not in (None, "stop"):
                raise ValueError("unexpected or incomplete completion")
            answer = choices[0]["message"]["content"]
            if type(answer) is not str or len(answer.encode("utf-8", "strict")) > 65536:
                raise ValueError("unexpected answer")
            if schema is None:
                return {"text": answer, "structured": None}
            structured = json.loads(answer, object_pairs_hook=_unique, parse_constant=_bad_constant)
            json.dumps(structured, ensure_ascii=False, allow_nan=False).encode("utf-8", "strict")
            Draft202012Validator(schema).validate(structured)
            if type(structured) is not dict:
                raise ValueError("structured response must be an object")
            return {"text": None, "structured": structured}
        except Exception as exc:
            raise ProviderError("Provider response is malformed, incomplete or failed schema validation") from exc
