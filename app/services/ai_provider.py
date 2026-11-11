import json
import logging
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dotenv import load_dotenv

load_dotenv(override=True)


class BaseAIProvider(ABC):
    """
    Abstract base class for AI/LLM providers.
    Supports Local LLM (Ollama, LM Studio, vLLM, llama.cpp) and Google Gemini API.
    """

    @property
    @abstractmethod
    def provider_name(self) -> str:
        pass

    @property
    @abstractmethod
    def model_name(self) -> str:
        pass

    @abstractmethod
    def generate_json(self, system_prompt: str, user_payload: dict, response_schema: dict | None = None) -> dict:
        """
        Send a system instruction and JSON payload to the model and return a parsed JSON dict.
        """
        pass


def _extract_json_from_text(raw_text: str) -> dict:
    """
    Safely parse JSON from model text output, handling markdown code fences
    and minor trailing-comma quirks from smaller local models.
    """
    if not raw_text:
        raise ValueError("Empty response received from AI model")

    cleaned = raw_text.strip()

    # Strip ```json ... ``` fences if the model wrapped the output
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    for candidate in (
        cleaned,
        cleaned[cleaned.find("{") : cleaned.rfind("}") + 1]
        if ("{" in cleaned and "}" in cleaned)
        else cleaned,
    ):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # Remove trailing commas before } or ] (common in smaller 3B local models)
            fixed = re.sub(r",\s*([}\]])", r"\1", candidate)
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                continue

    raise ValueError("AI model did not return valid JSON")


def _normalize_model_id(model_name: str) -> str:
    """
    Normalize Gemini model identifier by stripping any leading 'models/' prefix.
    """
    cleaned = (model_name or "").strip()
    if cleaned.startswith("models/"):
        cleaned = cleaned[len("models/"):]
    return cleaned


def _normalize_local_llm_url(raw_url: str) -> str:
    """
    Normalize LOCAL_LLM_URL so values like:
      - http://localhost:11434/v1       -> http://localhost:11434/v1/chat/completions
      - http://localhost:11434          -> http://localhost:11434/v1/chat/completions
      - http://localhost:11434/api/chat -> http://localhost:11434/api/chat
    all work automatically out of the box.
    """
    url = (raw_url or "http://localhost:11434/v1/chat/completions").strip().rstrip("/")
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.rstrip("/")

    if path == "" or path == "/":
        return f"{url}/v1/chat/completions"
    if path == "/v1":
        return f"{url}/chat/completions"
    return url


class GeminiAIProvider(BaseAIProvider):
    """
    Google Gemini API provider using REST API (zero external SDK dependency issues).
    Includes automatic model fallback & discovery if a model is deprecated, returns 404, or times out.
    """

    FALLBACK_MODELS = [
        "gemini-3.8-flash",
        "gemini-2.0-flash",
        "gemini-flash-latest",
        "gemini-2.5-flash-lite",
        "gemini-pro-latest",
    ]

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int = 180,
    ):
        load_dotenv(override=True)
        self.api_key = (api_key or os.getenv("GEMINI_API_KEY") or "").strip()
        raw_model = model or os.getenv("GEMINI_MODEL") or "gemini-3.8-flash"
        self._model = _normalize_model_id(raw_model)
        self.timeout = int(os.getenv("AI_TIMEOUT_SECONDS", str(timeout)))
        self.base_url = (
            os.getenv("GEMINI_API_BASE_URL")
            or "https://generativelanguage.googleapis.com/v1beta"
        ).rstrip("/")

    @property
    def provider_name(self) -> str:
        return "gemini"

    @property
    def model_name(self) -> str:
        return self._model

    def _discover_available_models(self) -> list[str]:
        list_url = f"{self.base_url}/models?key={self.api_key}"
        req = urllib.request.Request(list_url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return []

        available = []
        for item in payload.get("models", []):
            methods = item.get("supportedGenerationMethods") or []
            if "generateContent" not in methods:
                continue
            name = _normalize_model_id(item.get("name", ""))
            if any(
                skip in name
                for skip in ("embedding", "imagen", "veo", "tts", "audio", "aqa", "bisheng")
            ):
                continue
            if name.startswith("gemini"):
                available.append(name)

        available.sort(
            key=lambda m: (
                0 if "flash" in m and "lite" not in m else
                1 if "flash" in m else
                2 if "pro" in m else
                3
            )
        )
        return available

    def _call_generate_content(
        self,
        model_id: str,
        system_prompt: str,
        user_payload: dict,
        use_thinking_budget_zero: bool = True,
    ) -> dict:
        url = f"{self.base_url}/models/{model_id}:generateContent?key={self.api_key}"

        generation_config: dict = {
            "temperature": float(os.getenv("AI_TEMPERATURE", "0.3")),
            "responseMimeType": "application/json",
        }

        if use_thinking_budget_zero:
            generation_config["thinkingConfig"] = {"thinkingBudget": 0}

        compact_json = json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))

        request_body = {
            "system_instruction": {
                "parts": [{"text": system_prompt}]
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "text": (
                                "Student anonymized skill profile JSON:\n"
                                + compact_json
                            )
                        }
                    ],
                }
            ],
            "generationConfig": generation_config,
        }

        data_bytes = json.dumps(request_body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def generate_json(
        self,
        system_prompt: str,
        user_payload: dict,
        response_schema: dict | None = None,
    ) -> dict:
        if not self.api_key:
            raise ValueError(
                "GEMINI_API_KEY is not configured. Please set GEMINI_API_KEY in your .env file or set AI_PROVIDER=local."
            )

        models_to_try = [self._model]
        last_error = None
        response_data = None
        tried_models = set()

        while models_to_try:
            candidate_model = _normalize_model_id(models_to_try.pop(0))
            if not candidate_model or candidate_model in tried_models:
                continue
            tried_models.add(candidate_model)

            try:
                response_data = self._call_generate_content(
                    model_id=candidate_model,
                    system_prompt=system_prompt,
                    user_payload=user_payload,
                    use_thinking_budget_zero=True,
                )
                self._model = candidate_model
                break

            except urllib.error.HTTPError as http_err:
                err_body = http_err.read().decode("utf-8", errors="replace")
                last_error = (http_err.code, err_body, http_err)

                if http_err.code == 400 and "thinking" in err_body.lower():
                    try:
                        response_data = self._call_generate_content(
                            model_id=candidate_model,
                            system_prompt=system_prompt,
                            user_payload=user_payload,
                            use_thinking_budget_zero=False,
                        )
                        self._model = candidate_model
                        break
                    except urllib.error.HTTPError as retry_err:
                        retry_body = retry_err.read().decode("utf-8", errors="replace")
                        raise RuntimeError(
                            f"Gemini API HTTP {retry_err.code} error: {retry_body}"
                        ) from retry_err
                    except (TimeoutError, socket.timeout) as timeout_err:
                        last_error = ("TIMEOUT", str(timeout_err), timeout_err)
                        continue

                if http_err.code == 404:
                    suggested_matches = re.findall(
                        r"models/([a-zA-Z0-9._-]+)", err_body
                    )
                    for suggested in suggested_matches:
                        norm_suggested = _normalize_model_id(suggested)
                        if norm_suggested not in tried_models and norm_suggested not in models_to_try:
                            models_to_try.insert(0, norm_suggested)

                    if len(tried_models) == 1:
                        discovered = self._discover_available_models()
                        for m in discovered + self.FALLBACK_MODELS:
                            norm_m = _normalize_model_id(m)
                            if norm_m not in tried_models and norm_m not in models_to_try:
                                models_to_try.append(norm_m)

                    logging.warning(
                        "Gemini model '%s' returned 404. Trying fallback models: %s",
                        candidate_model,
                        models_to_try[:5],
                    )
                    continue

                raise RuntimeError(
                    f"Gemini API HTTP {http_err.code} error: {err_body}"
                ) from http_err

            except (TimeoutError, socket.timeout) as timeout_err:
                last_error = ("TIMEOUT", f"Request to {candidate_model} timed out after {self.timeout}s", timeout_err)
                logging.warning(
                    "Gemini model '%s' timed out after %ss.",
                    candidate_model,
                    self.timeout,
                )
                if len(tried_models) == 1:
                    discovered = self._discover_available_models()
                    for m in discovered + self.FALLBACK_MODELS:
                        norm_m = _normalize_model_id(m)
                        if norm_m not in tried_models and norm_m not in models_to_try:
                            models_to_try.append(norm_m)
                continue

            except urllib.error.URLError as url_err:
                if isinstance(getattr(url_err, "reason", None), (TimeoutError, socket.timeout)):
                    last_error = ("TIMEOUT", f"Connection to {candidate_model} timed out", url_err)
                    continue
                raise RuntimeError(
                    f"Failed to connect to Gemini API: {url_err.reason}"
                ) from url_err

        if response_data is None:
            if last_error:
                code, err_body, orig_exc = last_error
                raise RuntimeError(
                    f"Gemini API {code} error (tried models {list(tried_models)}): {err_body}"
                ) from orig_exc
            raise RuntimeError("Gemini API failed to return a response")

        candidates = response_data.get("candidates") or []
        if not candidates:
            raise RuntimeError(f"Gemini API returned no candidates: {response_data}")

        parts = candidates[0].get("content", {}).get("parts") or []
        text_content = None
        for part in reversed(parts):
            if "text" in part and not part.get("thought", False):
                text_content = part["text"]
                break
        if text_content is None and parts and "text" in parts[-1]:
            text_content = parts[-1]["text"]

        if not text_content:
            raise RuntimeError("Gemini API response did not contain text output")

        return _extract_json_from_text(text_content)


class LocalLLMProvider(BaseAIProvider):
    """
    Local LLM provider supporting Ollama, LM Studio, vLLM, llama.cpp,
    or any OpenAI-compatible local endpoint.

    Configured via .env:
        AI_PROVIDER=local
        LOCAL_LLM_URL=http://localhost:11434/v1
        LOCAL_LLM_MODEL=llama3.2:3b
        LOCAL_LLM_API_KEY=ollama
    """

    def __init__(
        self,
        endpoint_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        timeout: int = 300,
    ):
        load_dotenv(override=True)
        raw_url = (
            endpoint_url
            or os.getenv("LOCAL_LLM_URL")
            or "http://localhost:11434/v1/chat/completions"
        )
        self.endpoint_url = _normalize_local_llm_url(raw_url)
        self._model = (
            model or os.getenv("LOCAL_LLM_MODEL") or "llama3.2:3b"
        ).strip()
        self.api_key = (api_key or os.getenv("LOCAL_LLM_API_KEY") or "ollama").strip()
        self.timeout = int(os.getenv("AI_TIMEOUT_SECONDS", str(timeout)))

    @property
    def provider_name(self) -> str:
        return "local_llm"

    @property
    def model_name(self) -> str:
        return self._model

    def _build_request_body(self, target_url: str, system_prompt: str, user_message: str, temperature: float) -> dict:
        if "/api/generate" in target_url:
            return {
                "model": self._model,
                "system": system_prompt,
                "prompt": user_message,
                "format": "json",
                "stream": False,
                "options": {
                    "temperature": temperature,
                    "num_ctx": 8192,
                },
            }
        if "/api/chat" in target_url:
            return {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "format": "json",
                "stream": False,
                "options": {
                    "temperature": temperature,
                    "num_ctx": 8192,
                },
            }
        # OpenAI-compatible endpoint (/v1/chat/completions)
        return {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": temperature,
            "max_tokens": 4096,
            "response_format": {"type": "json_object"},
        }

    def generate_json(
        self,
        system_prompt: str,
        user_payload: dict,
        response_schema: dict | None = None,
    ) -> dict:
        compact_json = json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))
        user_message = (
            "Analyze the following anonymized student skill profile JSON "
            "and return ONLY valid JSON with `career_path` and `top_10_jobs` (10 job items):\n"
            + compact_json
        )

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        temperature = float(os.getenv("AI_TEMPERATURE", "0.3"))

        # Try primary normalized URL; if /v1/chat/completions returns 404, also try Ollama native /api/chat
        parsed = urllib.parse.urlparse(self.endpoint_url)
        base_host = f"{parsed.scheme}://{parsed.netloc}"
        candidate_urls = [self.endpoint_url]
        ollama_chat_url = f"{base_host}/api/chat"
        if ollama_chat_url not in candidate_urls:
            candidate_urls.append(ollama_chat_url)

        response_data = None
        last_error = None

        for target_url in candidate_urls:
            request_body = self._build_request_body(
                target_url=target_url,
                system_prompt=system_prompt,
                user_message=user_message,
                temperature=temperature,
            )
            data_bytes = json.dumps(request_body).encode("utf-8")
            req = urllib.request.Request(
                target_url,
                data=data_bytes,
                headers=headers,
                method="POST",
            )

            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    response_data = json.loads(resp.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as http_err:
                err_body = http_err.read().decode("utf-8", errors="replace")
                last_error = (
                    f"Local LLM HTTP {http_err.code} error at {target_url}: {err_body}",
                    http_err,
                )
                if http_err.code == 404 and target_url != candidate_urls[-1]:
                    continue
                raise RuntimeError(last_error[0]) from http_err
            except (TimeoutError, socket.timeout) as timeout_err:
                raise RuntimeError(
                    f"Local LLM request timed out after {self.timeout}s at {target_url} (model: {self._model})"
                ) from timeout_err
            except urllib.error.URLError as url_err:
                raise RuntimeError(
                    f"Failed to connect to Local LLM at {target_url}. Ensure Ollama/Local LLM server is running: {url_err.reason}"
                ) from url_err

        if response_data is None:
            if last_error:
                raise RuntimeError(last_error[0]) from last_error[1]
            raise RuntimeError("Local LLM failed to return a response")

        if "choices" in response_data and response_data["choices"]:
            text_output = (
                response_data["choices"][0]
                .get("message", {})
                .get("content", "")
            )
        elif "message" in response_data and isinstance(response_data["message"], dict):
            text_output = response_data["message"].get("content", "")
        elif "response" in response_data:
            text_output = response_data.get("response", "")
        else:
            raise RuntimeError(f"Unrecognized Local LLM response format: {response_data}")

        return _extract_json_from_text(text_output)


def get_ai_provider(provider_override: str | None = None) -> BaseAIProvider:
    """
    Factory function returning the configured AI provider.
    Controlled by AI_PROVIDER in .env:
      - 'local' / 'ollama' / 'local_llm': Uses Local LLM endpoint (default if GEMINI_API_KEY is unset)
      - 'gemini': Uses Google Gemini API
    """
    load_dotenv(override=True)
    provider = (provider_override or os.getenv("AI_PROVIDER") or "").strip().lower()

    if provider in ("local", "local_llm", "ollama", "vllm", "lmstudio"):
        return LocalLLMProvider()

    if provider == "gemini" and (os.getenv("GEMINI_API_KEY") or "").strip():
        return GeminiAIProvider()

    # If GEMINI_API_KEY is commented out or empty, automatically use Local LLM
    if not (os.getenv("GEMINI_API_KEY") or "").strip():
        return LocalLLMProvider()

    return GeminiAIProvider()
