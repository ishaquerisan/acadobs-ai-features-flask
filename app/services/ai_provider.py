import json
import logging
import os
import re
import socket
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dotenv import load_dotenv

load_dotenv(override=True)


class BaseAIProvider(ABC):
    """
    Abstract base class for AI/LLM providers.
    Allows seamless switching between Google Gemini API now
    and a Local LLM (Ollama, vLLM, LM Studio, llama.cpp) in the future.
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
    Safely parse JSON from model text output, handling markdown code fences if present.
    """
    if not raw_text:
        raise ValueError("Empty response received from AI model")

    cleaned = raw_text.strip()

    # Strip ```json ... ``` fences if the model wrapped the output
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Try locating the first '{' and last '}'
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise ValueError("AI model did not return valid JSON")


def _normalize_model_id(model_name: str) -> str:
    """
    Normalize Gemini model identifier by stripping any leading 'models/' prefix.
    """
    cleaned = (model_name or "").strip()
    if cleaned.startswith("models/"):
        cleaned = cleaned[len("models/"):]
    return cleaned


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
        """
        Query the Gemini ListModels endpoint to find models available to this API key
        that support `generateContent`.
        """
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

        # Use responseMimeType="application/json" without heavy responseSchema grammar compilation
        # so Gemini responds in a few seconds instead of timing out.
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
                "GEMINI_API_KEY is not configured. Please set GEMINI_API_KEY in your .env file."
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

                # If model does not support thinkingConfig (HTTP 400), retry immediately without thinkingConfig
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

                # If HTTP 404 (model deprecated / unavailable to new users), extract suggested model & discover available models
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
        # Find the part containing JSON text (skipping any thought parts if present)
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
    Local LLM provider for future migration (supports Ollama, LM Studio, vLLM, llama.cpp,
    or any OpenAI-compatible local endpoint).

    Switch to this provider anytime by setting in .env:
        AI_PROVIDER=local
        LOCAL_LLM_URL=http://localhost:11434/v1/chat/completions  (or http://localhost:11434/api/generate)
        LOCAL_LLM_MODEL=llama3.1
    """

    def __init__(
        self,
        endpoint_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        timeout: int = 180,
    ):
        load_dotenv(override=True)
        self.endpoint_url = (
            endpoint_url
            or os.getenv("LOCAL_LLM_URL")
            or "http://localhost:11434/v1/chat/completions"
        ).strip()
        self._model = (
            model or os.getenv("LOCAL_LLM_MODEL") or "llama3.1"
        ).strip()
        self.api_key = (api_key or os.getenv("LOCAL_LLM_API_KEY") or "").strip()
        self.timeout = int(os.getenv("AI_TIMEOUT_SECONDS", str(timeout)))

    @property
    def provider_name(self) -> str:
        return "local_llm"

    @property
    def model_name(self) -> str:
        return self._model

    def generate_json(
        self,
        system_prompt: str,
        user_payload: dict,
        response_schema: dict | None = None,
    ) -> dict:
        compact_json = json.dumps(user_payload, ensure_ascii=False, separators=(",", ":"))
        user_message = (
            "Analyze the following anonymized student skill profile JSON "
            "and return ONLY valid JSON matching the required career path and 10 jobs schema:\n"
            + compact_json
        )

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        temperature = float(os.getenv("AI_TEMPERATURE", "0.3"))

        if "/api/generate" in self.endpoint_url:
            request_body = {
                "model": self._model,
                "system": system_prompt,
                "prompt": user_message,
                "format": "json",
                "stream": False,
                "options": {"temperature": temperature},
            }
        elif "/api/chat" in self.endpoint_url:
            request_body = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "format": "json",
                "stream": False,
                "options": {"temperature": temperature},
            }
        else:
            request_body = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "temperature": temperature,
                "response_format": {"type": "json_object"},
            }

        data_bytes = json.dumps(request_body).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint_url,
            data=data_bytes,
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                response_data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as http_err:
            err_body = http_err.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Local LLM HTTP {http_err.code} error: {err_body}"
            ) from http_err
        except (TimeoutError, socket.timeout) as timeout_err:
            raise RuntimeError(
                f"Local LLM request timed out after {self.timeout}s at {self.endpoint_url}"
            ) from timeout_err
        except urllib.error.URLError as url_err:
            raise RuntimeError(
                f"Failed to connect to Local LLM at {self.endpoint_url}: {url_err.reason}"
            ) from url_err

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
      - 'gemini' (default): Uses Google Gemini API
      - 'local' / 'ollama' / 'local_llm': Uses Local LLM endpoint
    """
    load_dotenv(override=True)
    provider = (provider_override or os.getenv("AI_PROVIDER") or "gemini").strip().lower()

    if provider in ("local", "local_llm", "ollama", "vllm", "lmstudio"):
        return LocalLLMProvider()

    return GeminiAIProvider()
