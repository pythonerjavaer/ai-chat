"""Provider boundary for Leap Realm's user-triggered interpretation.

Translation deliberately lives elsewhere.  These providers only receive the
small source range and context that the user explicitly asks to interpret.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol


OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
GEMINI_GENERATE_CONTENT_URL = "https://generativelanguage.googleapis.com/v1beta"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class InterpretationProviderResult:
    text: str
    provider: str
    requested_model: str
    actual_model: str
    requested_at: str
    usage: dict[str, int]


class InterpretationProvider(Protocol):
    provider_id: str
    label: str
    requested_model: str
    fallback_model: str
    is_free: bool
    configured: bool

    def generate(
        self, user_id: int, system_prompt: str, prompt: str, max_output_tokens: int,
    ) -> InterpretationProviderResult: ...


class InterpretationProviderError(RuntimeError):
    def __init__(self, code: str, status_code: int, public_message: str):
        super().__init__(public_message)
        self.code = code
        self.status_code = status_code
        self.public_message = public_message


class CallbackInterpretationProvider:
    """Adapter for Frostfire's existing OpenAI runner and usage accounting."""

    provider_id = "openai"
    label = "OpenAI"
    is_free = False

    def __init__(
        self,
        runner: Callable[[int, str, str, int], dict[str, Any]] | None,
        model: str,
        provider_id: str = "openai",
    ):
        self.provider_id = provider_id
        self.label = "OpenAI" if provider_id == "openai" else "冰焰AI"
        self.runner = runner
        self.requested_model = model
        self.fallback_model = ""
        self.configured = runner is not None

    def generate(
        self, user_id: int, system_prompt: str, prompt: str, max_output_tokens: int,
    ) -> InterpretationProviderResult:
        if self.runner is None:
            raise InterpretationProviderError(
                "AI_NOT_CONFIGURED", 503, "冰焰现有OpenAI服务尚未配置。",
            )
        result = self.runner(user_id, system_prompt, prompt, max_output_tokens)
        text = str(result.get("text") or "").strip()
        if not text:
            raise InterpretationProviderError(
                "INTERPRETATION_PARSE_ERROR", 502, "OpenAI没有返回可用的内容解读结果。",
            )
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        return InterpretationProviderResult(
            text=text,
            provider=self.provider_id,
            requested_model=self.requested_model,
            actual_model=str(result.get("model") or self.requested_model),
            requested_at=_now(),
            usage={key: int(usage.get(key, 0) or 0) for key in ("input_tokens", "output_tokens", "total_tokens")},
        )


class OpenRouterInterpretationProvider:
    provider_id = "openrouter"
    label = "免费：OpenRouter Free"
    is_free = True

    def __init__(
        self,
        api_key: str,
        model: str = "openrouter/free",
        *,
        fallback_model: str = "openrouter/free",
        endpoint: str = OPENROUTER_CHAT_COMPLETIONS_URL,
        site_url: str = "",
        timeout_seconds: int = 60,
        opener: Callable[..., Any] | None = None,
    ):
        self.api_key = api_key.strip()
        self.requested_model = model.strip() or "openrouter/free"
        self.fallback_model = fallback_model.strip()
        if self.fallback_model == self.requested_model:
            self.fallback_model = ""
        self.endpoint = endpoint.strip() or OPENROUTER_CHAT_COMPLETIONS_URL
        self.site_url = site_url.strip()
        self.timeout_seconds = max(10, min(120, int(timeout_seconds)))
        self.opener = opener or urllib.request.urlopen
        self.configured = bool(self.api_key)
        self._cooldown_until = 0.0
        self._cooldown_lock = threading.Lock()

    def _set_cooldown(self, seconds: int = 30) -> None:
        with self._cooldown_lock:
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + max(5, seconds))

    def _in_cooldown(self) -> bool:
        with self._cooldown_lock:
            return time.monotonic() < self._cooldown_until

    def _error(self, status_code: int) -> InterpretationProviderError:
        if status_code == 429:
            return InterpretationProviderError(
                "OPENROUTER_RATE_LIMITED", 429,
                "OpenRouter免费路由当前达到速率限制，请稍后重试或手动切换其他解读引擎。",
            )
        if status_code in {408, 502, 503, 504}:
            return InterpretationProviderError(
                "OPENROUTER_UNAVAILABLE", 503,
                "OpenRouter免费路由当前不可用，请稍后重试。",
            )
        return InterpretationProviderError(
            "OPENROUTER_PROVIDER_ERROR", 502,
            "OpenRouter未能完成本次内容解读请求。",
        )

    def _generate_once(
        self, model: str, system_prompt: str, prompt: str, max_output_tokens: int,
    ) -> InterpretationProviderResult:
        body = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": max_output_tokens,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-Title": "Frostfire Leap Reading Assistant",
        }
        if self.site_url:
            headers["HTTP-Referer"] = self.site_url
        request = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            # Consume at most a small amount so the connection can close, but
            # never log or return the provider body.
            try:
                exc.read(16_384)
            except Exception:
                pass
            error = self._error(int(exc.code or 502))
            if error.code == "OPENROUTER_RATE_LIMITED":
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    self._set_cooldown(min(300, max(30, int(float(retry_after)))))
                except (TypeError, ValueError):
                    self._set_cooldown()
            raise error from exc
        except (TimeoutError, socket.timeout) as exc:
            raise InterpretationProviderError(
                "OPENROUTER_UNAVAILABLE", 503,
                "OpenRouter免费路由响应超时，请稍后重试。",
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise InterpretationProviderError(
                "OPENROUTER_UNAVAILABLE", 503,
                "OpenRouter免费路由当前无法连接，请稍后重试。",
            ) from exc
        if len(raw) > 2 * 1024 * 1024:
            raise InterpretationProviderError(
                "OPENROUTER_PARSE_ERROR", 502,
                "OpenRouter返回内容超过安全处理上限。",
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
            content = payload["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
            text = str(content or "").strip()
            actual_model = str(payload.get("model") or "").strip()
            if not text or not actual_model:
                raise ValueError("missing response content or model")
            candidate = text
            if candidate.startswith("```"):
                lines = candidate.splitlines()
                if lines and lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                candidate = "\n".join(lines).strip()
            if not isinstance(json.loads(candidate), dict):
                raise ValueError("response content is not a JSON object")
            usage_payload = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise InterpretationProviderError(
                "OPENROUTER_PARSE_ERROR", 502,
                "OpenRouter返回格式无法解析，请稍后重试。",
            ) from exc
        return InterpretationProviderResult(
            text=text,
            provider=self.provider_id,
            requested_model=self.requested_model,
            actual_model=actual_model,
            requested_at=_now(),
            usage={
                "input_tokens": int(usage_payload.get("prompt_tokens", 0) or 0),
                "output_tokens": int(usage_payload.get("completion_tokens", 0) or 0),
                "total_tokens": int(usage_payload.get("total_tokens", 0) or 0),
            },
        )

    def generate(
        self, user_id: int, system_prompt: str, prompt: str, max_output_tokens: int,
    ) -> InterpretationProviderResult:
        del user_id
        if not self.configured:
            raise InterpretationProviderError(
                "OPENROUTER_NOT_CONFIGURED", 503,
                "OpenRouter免费解读尚未配置服务端API Key。",
            )
        if self._in_cooldown():
            raise InterpretationProviderError("OPENROUTER_RATE_LIMITED", 429, "OpenRouter免费服务正在冷却，请稍后重试。")
        try:
            return self._generate_once(
                self.requested_model, system_prompt, prompt, max_output_tokens,
            )
        except InterpretationProviderError as primary_error:
            if not self.fallback_model:
                raise
            try:
                return self._generate_once(
                    self.fallback_model, system_prompt, prompt, max_output_tokens,
                )
            except InterpretationProviderError as fallback_error:
                # Preserve a useful category from the final free attempt.  No
                # paid provider is ever selected by this fallback path.
                raise fallback_error from primary_error


class GeminiInterpretationProvider:
    """Gemini Developer API adapter, configured explicitly for a free-tier model.

    The provider never changes the configured model and never selects a paid
    model.  A missing model/key therefore makes it unavailable rather than
    silently guessing a billable route.
    """

    provider_id = "gemini"
    label = "免费：Gemini"
    is_free = True

    def __init__(
        self,
        api_key: str,
        model: str = "",
        *,
        endpoint: str = GEMINI_GENERATE_CONTENT_URL,
        allow_paid: bool = False,
        timeout_seconds: int = 60,
        opener: Callable[..., Any] | None = None,
    ):
        self.api_key = api_key.strip()
        self.requested_model = model.strip()
        self.fallback_model = ""
        self.endpoint = endpoint.strip().rstrip("/") or GEMINI_GENERATE_CONTENT_URL
        self.allow_paid = bool(allow_paid)
        self.timeout_seconds = max(10, min(120, int(timeout_seconds)))
        self.opener = opener or urllib.request.urlopen
        # A model must be supplied by deployment configuration after checking
        # the account's ListModels response.  This avoids silently selecting a
        # model whose billing tier is unknown.
        self.configured = bool(self.api_key and self.requested_model and self.allow_paid is False)
        self._cooldown_until = 0.0
        self._cooldown_lock = threading.Lock()

    def _set_cooldown(self, seconds: int = 30) -> None:
        with self._cooldown_lock:
            self._cooldown_until = max(self._cooldown_until, time.monotonic() + max(5, seconds))

    def _in_cooldown(self) -> bool:
        with self._cooldown_lock:
            return time.monotonic() < self._cooldown_until

    def list_models(self) -> list[dict[str, Any]]:
        """Read the account's model catalogue without selecting a model."""
        if not self.api_key:
            raise InterpretationProviderError("GEMINI_NOT_CONFIGURED", 503, "Gemini免费解读尚未配置API Key。")
        request = urllib.request.Request(
            f"{self.endpoint}/models?key={urllib.parse.quote(self.api_key, safe='')}",
            headers={"Accept": "application/json"}, method="GET",
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read(512 * 1024).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise self._error(int(exc.code or 502)) from exc
        except (OSError, socket.timeout, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            raise InterpretationProviderError("GEMINI_PROVIDER_ERROR", 503, "Gemini模型列表暂时无法读取。") from exc
        models = payload.get("models", []) if isinstance(payload, dict) else []
        return [item for item in models if isinstance(item, dict)]

    @staticmethod
    def _error(status_code: int, body: str = "") -> InterpretationProviderError:
        marker = body.casefold()
        if status_code == 429:
            code = "GEMINI_FREE_QUOTA_EXHAUSTED" if any(x in marker for x in ("resource_exhausted", "quota", "daily limit")) else "GEMINI_RATE_LIMITED"
            message = "Gemini免费额度暂时耗尽，请稍后重试或切换其他免费引擎。" if code.endswith("EXHAUSTED") else "Gemini免费服务当前达到速率限制，请稍后重试。"
            return InterpretationProviderError(code, 429, message)
        if status_code in {401, 403}:
            return InterpretationProviderError("GEMINI_PERMISSION_DENIED", 503, "Gemini免费服务未授权或当前账号不可用。")
        if status_code in {404}:
            return InterpretationProviderError("GEMINI_MODEL_UNAVAILABLE", 503, "配置的Gemini模型当前不可用。")
        if status_code in {408, 502, 503, 504}:
            return InterpretationProviderError("GEMINI_TIMEOUT" if status_code == 408 else "GEMINI_PROVIDER_ERROR", 503, "Gemini免费服务当前不可用，请稍后重试。")
        return InterpretationProviderError("GEMINI_PROVIDER_ERROR", 502, "Gemini免费服务未能完成本次内容解读请求。")

    def _generate_once(self, system_prompt: str, prompt: str, max_output_tokens: int) -> InterpretationProviderResult:
        url = f"{self.endpoint}/models/{urllib.parse.quote(self.requested_model, safe='')}:generateContent?key={urllib.parse.quote(self.api_key, safe='')}"
        body = json.dumps({
            "system_instruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": max_output_tokens,
                "responseMimeType": "application/json",
            },
        }, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            try:
                body_text = exc.read(16_384).decode("utf-8", "ignore")
            except Exception:
                body_text = ""
            error = self._error(int(exc.code or 502), body_text)
            if error.status_code == 429:
                self._set_cooldown()
            raise error from exc
        except (TimeoutError, socket.timeout) as exc:
            self._set_cooldown(15)
            raise InterpretationProviderError("GEMINI_TIMEOUT", 504, "Gemini免费服务响应超时，请稍后重试。") from exc
        except (urllib.error.URLError, OSError) as exc:
            self._set_cooldown(15)
            raise InterpretationProviderError("GEMINI_PROVIDER_ERROR", 503, "Gemini免费服务当前无法连接，请稍后重试。") from exc
        if len(raw) > 2 * 1024 * 1024:
            raise InterpretationProviderError("GEMINI_PARSE_ERROR", 502, "Gemini返回内容超过安全处理上限。")
        try:
            payload = json.loads(raw.decode("utf-8"))
            parts = payload["candidates"][0]["content"]["parts"]
            text = "".join(str(part.get("text") or "") for part in parts if isinstance(part, dict)).strip()
            actual_model = str(payload.get("modelVersion") or payload.get("model") or self.requested_model).strip()
            candidate = text[3:-3].strip() if text.startswith("```") and text.endswith("```") else text
            if not text or not isinstance(json.loads(candidate), dict):
                raise ValueError("missing structured response")
            usage_payload = payload.get("usageMetadata") if isinstance(payload.get("usageMetadata"), dict) else {}
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise InterpretationProviderError("GEMINI_PARSE_ERROR", 502, "Gemini返回格式无法解析，请稍后重试。") from exc
        return InterpretationProviderResult(
            text=text, provider=self.provider_id, requested_model=self.requested_model,
            actual_model=actual_model, requested_at=_now(),
            usage={
                "input_tokens": int(usage_payload.get("promptTokenCount", 0) or 0),
                "output_tokens": int(usage_payload.get("candidatesTokenCount", 0) or 0),
                "total_tokens": int(usage_payload.get("totalTokenCount", 0) or 0),
            },
        )

    def generate(self, user_id: int, system_prompt: str, prompt: str, max_output_tokens: int) -> InterpretationProviderResult:
        del user_id
        if not self.configured:
            raise InterpretationProviderError("GEMINI_NOT_CONFIGURED", 503, "Gemini免费解读尚未配置API Key和已核验模型。")
        if self._in_cooldown():
            raise InterpretationProviderError("GEMINI_RATE_LIMITED", 429, "Gemini免费服务正在冷却，请稍后重试。")
        return self._generate_once(system_prompt, prompt, max_output_tokens)


class FreeInterpretationProvider:
    """Ordered free-only router.  It never contains OpenAI."""

    provider_id = "auto"
    label = "免费：自动"
    is_free = True

    def __init__(self, providers: list[InterpretationProvider]):
        self.providers = providers
        self.requested_model = "free-auto"
        self.fallback_model = ""
        self.configured = any(item.configured for item in providers)

    def generate(self, user_id: int, system_prompt: str, prompt: str, max_output_tokens: int) -> InterpretationProviderResult:
        last_error: InterpretationProviderError | None = None
        for provider in self.providers:
            if not provider.configured:
                continue
            try:
                return provider.generate(user_id, system_prompt, prompt, max_output_tokens)
            except InterpretationProviderError as exc:
                last_error = exc
                if exc.code not in {
                    "OPENROUTER_RATE_LIMITED", "OPENROUTER_UNAVAILABLE", "OPENROUTER_PROVIDER_ERROR",
                    "OPENROUTER_PARSE_ERROR", "GEMINI_RATE_LIMITED", "GEMINI_FREE_QUOTA_EXHAUSTED",
                    "GEMINI_UNAVAILABLE", "GEMINI_TIMEOUT", "GEMINI_PROVIDER_ERROR", "GEMINI_MODEL_UNAVAILABLE",
                }:
                    raise
        raise last_error or InterpretationProviderError("ALL_FREE_INTERPRETATION_PROVIDERS_UNAVAILABLE", 503, "所有免费内容解读服务当前不可用，请稍后重试。")
