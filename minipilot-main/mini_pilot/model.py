"""Model client for MiniPilot.

The model layer sends the user task and current screenshot to an OpenAI
compatible vision-language model, then returns the model's text response.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any
from urllib import error, request

from mini_pilot.screenshot import Screenshot


@dataclass(frozen=True)
class ModelConfig:
    """Configuration for an OpenAI compatible model endpoint."""

    base_url: str = "http://localhost:8000/v1"
    model_name: str = "autoglm-phone-9b"
    api_key: str = "EMPTY"
    temperature: float = 0.0
    max_tokens: int = 1024
    image_max_side: int = 2048
    extra_body: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "ModelConfig":
        """Create config from environment variables with local defaults."""
        return cls(
            base_url=os.getenv("MINI_PILOT_BASE_URL", cls.base_url),
            model_name=os.getenv("MINI_PILOT_MODEL", cls.model_name),
            api_key=os.getenv("MINI_PILOT_API_KEY", cls.api_key),
            temperature=float(os.getenv("MINI_PILOT_TEMPERATURE", cls.temperature)),
            max_tokens=int(os.getenv("MINI_PILOT_MAX_TOKENS", cls.max_tokens)),
            image_max_side=int(
                os.getenv("MINI_PILOT_IMAGE_MAX_SIDE", cls.image_max_side)
            ),
        )


@dataclass(frozen=True)
class ModelResponse:
    """Text returned by the model."""

    content: str


@dataclass(frozen=True)
class ModelHistoryItem:
    """One previous step passed back to the model as context."""

    step: int
    screenshot: Screenshot
    model_output: str
    action_result: str


class ModelError(RuntimeError):
    """Raised when the model request fails or returns an invalid response."""


class ModelClient:
    """Small wrapper around an OpenAI compatible chat completions API."""

    def __init__(self, config: ModelConfig | None = None):
        self.config = config or ModelConfig.from_env()
        try:
            self.config.api_key.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ModelError(
                "API key must be the real ASCII token from your model provider. "
                "Replace placeholders like `你的key` before running MiniPilot."
            ) from exc

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ModelError(
                "The openai package is required for model requests. "
                "Install project dependencies first: pip install -r requirements.txt"
            ) from exc

        self.client = OpenAI(base_url=self.config.base_url, api_key=self.config.api_key)

    def ask(
        self,
        task: str,
        screenshot: Screenshot,
        system_prompt: str,
        previous_action: str | None = None,
        history: list[ModelHistoryItem] | None = None,
    ) -> ModelResponse:
        """Ask the model what to do next.

        Args:
            task: User's high-level task.
            screenshot: Current phone screenshot.
            system_prompt: Instructions that define the action output format.
            previous_action: Optional text describing what happened last step.
            history: Optional previous step records, including screenshots.

        Returns:
            ModelResponse containing the model's raw text.
        """
        messages = self._build_messages(
            task=task,
            screenshot=screenshot,
            system_prompt=system_prompt,
            previous_action=previous_action,
            history=history,
        )

        try:
            response = self.client.chat.completions.create(
                model=self.config.model_name,
                messages=messages,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
                extra_body=self.config.extra_body,
                stream=False,
            )
        except Exception as exc:
            if "Extra data" in str(exc):
                return self._ask_with_raw_http(messages)
            raise ModelError(f"Model request failed: {_format_model_exception(exc)}") from exc

        try:
            content = _extract_sdk_response_content(response).strip()
        except Exception as exc:
            raise ModelError(f"Could not parse model response: {exc}") from exc

        if not content:
            raise ModelError("Model returned an empty response.")

        return ModelResponse(content=content)

    def ask_text(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> ModelResponse:
        """Ask the model with text-only messages."""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        try:
            response = self.client.chat.completions.create(
                model=self.config.model_name,
                messages=messages,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
                extra_body=self.config.extra_body,
                stream=False,
            )
        except Exception as exc:
            if "Extra data" in str(exc):
                return self._ask_with_raw_http(messages)
            raise ModelError(f"Model request failed: {_format_model_exception(exc)}") from exc

        content = _extract_sdk_response_content(response).strip()
        if not content:
            raise ModelError("Model returned an empty response.")

        return ModelResponse(content=content)

    def ask_messages(self, messages: list[dict[str, Any]]) -> ModelResponse:
        """Ask the model with a caller-managed conversation history."""
        last_error: Exception | None = None
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.config.model_name,
                    messages=messages,
                    temperature=self.config.temperature,
                    max_tokens=self.config.max_tokens,
                    extra_body=self.config.extra_body,
                    stream=False,
                )
            except Exception as exc:
                if "Extra data" in str(exc):
                    return self._ask_with_raw_http(messages)
                last_error = exc
            else:
                try:
                    content = _extract_sdk_response_content(response).strip()
                except Exception as exc:
                    last_error = exc
                else:
                    if content:
                        return ModelResponse(content=content)
                    last_error = ModelError("Model returned an empty response.")

            if attempt < max_attempts:
                time.sleep(float(attempt))

        if last_error is not None:
            if isinstance(last_error, ModelError):
                raise last_error
            if "Could not read SDK model response content" in str(last_error):
                raise ModelError(f"Could not parse model response after retry: {last_error}") from last_error
            raise ModelError(f"Model request failed after retry: {_format_model_exception(last_error)}") from last_error

        raise ModelError("Model request failed after retry.")

    def _ask_with_raw_http(self, messages: list[dict[str, Any]]) -> ModelResponse:
        """Fallback for compatible providers that return non-SDK JSON bodies."""
        endpoint = self.config.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.config.model_name,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        payload.update(self.config.extra_body)

        http_request = request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )

        try:
            with request.urlopen(http_request, timeout=120) as response:
                body = response.read().decode("utf-8", errors="replace")
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ModelError(f"Model HTTP request failed: {exc.code} {body}") from exc
        except error.URLError as exc:
            raise ModelError(f"Model HTTP request failed: {exc}") from exc

        content = _extract_message_content(_parse_model_response_body(body))
        if not content:
            raise ModelError(f"Model returned an empty response. Raw body: {body[:500]}")

        return ModelResponse(content=content.strip())

    @staticmethod
    def _build_messages(
        task: str,
        screenshot: Screenshot,
        system_prompt: str,
        previous_action: str | None = None,
        history: list[ModelHistoryItem] | None = None,
    ) -> list[dict[str, Any]]:
        """Build OpenAI chat messages with text and screenshot image."""
        user_text = f"Task: {task}\n\nScreen size: {screenshot.width}x{screenshot.height}"
        if previous_action:
            user_text += f"\n\nPrevious action result: {previous_action}"

        content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]

        if history:
            for item in history:
                content.append(
                    {
                        "type": "text",
                        "text": (
                            f"History step {item.step}:\n"
                            f"Model output: {_compact_history_text(item.model_output)}\n"
                            f"Action result: {item.action_result}"
                        ),
                    }
                )
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": _image_data_url(item.screenshot),
                        },
                    }
                )

        content.append({"type": "text", "text": "Current screenshot:"})
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": _image_data_url(screenshot),
                },
            }
        )

        return [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": content,
            },
        ]


def _image_data_url(screenshot: Screenshot) -> str:
    """Build a data URL for a screenshot."""
    return f"data:{screenshot.mime_type};base64,{screenshot.base64_data}"


def _compact_history_text(text: str, limit: int = 240) -> str:
    """Keep history useful without feeding long failed reasoning back to the model."""
    stripped = " ".join(text.strip().split())
    for prefix in ("do(", "finish("):
        start = stripped.find(prefix)
        if start != -1:
            return stripped[start : start + limit]
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 3] + "..."


def _parse_model_response_body(body: str) -> dict[str, Any]:
    """Parse regular JSON, concatenated JSON, or simple SSE data responses."""
    stripped = body.strip()
    if not stripped:
        raise ModelError("Model returned an empty HTTP response.")

    if stripped.startswith("data:"):
        chunks: list[dict[str, Any]] = []
        for line in stripped.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue

            value = line.removeprefix("data:").strip()
            if not value or value == "[DONE]":
                continue
            chunks.append(json.loads(value))

        if not chunks:
            raise ModelError("Model returned an SSE response without JSON data.")
        return _combine_response_objects(chunks)

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        objects: list[dict[str, Any]] = []
        index = 0
        while index < len(stripped):
            while index < len(stripped) and stripped[index].isspace():
                index += 1

            if index >= len(stripped):
                break

            data, next_index = decoder.raw_decode(stripped, index)
            if not isinstance(data, dict):
                raise ModelError("Model response JSON is not an object.")
            objects.append(data)
            index = next_index

        if not objects:
            raise ModelError("Model response did not contain JSON objects.")
        return _combine_response_objects(objects)


def _extract_message_content(data: dict[str, Any]) -> str:
    """Extract assistant message text from an OpenAI-compatible response."""
    if "error" in data:
        raise ModelError(f"Model returned an error: {data['error']}")

    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelError(f"Could not read model response content: {data}") from exc

    delta = choice.get("delta")
    if isinstance(delta, dict) and isinstance(delta.get("content"), str):
        return delta["content"]

    message = choice.get("message")
    if not isinstance(message, dict):
        return ""

    content = message.get("content")

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                text_parts.append(item["text"])
        return "\n".join(text_parts)

    return str(content)


def _combine_response_objects(objects: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine chunk-style response objects into one message response."""
    if len(objects) == 1:
        return objects[0]

    delta_parts: list[str] = []
    message_parts: list[str] = []

    for data in objects:
        if "error" in data:
            return data

        try:
            choice = data["choices"][0]
        except (KeyError, IndexError, TypeError):
            continue

        delta = choice.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
            delta_parts.append(delta["content"])

        message = choice.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            message_parts.append(message["content"])

    content = "".join(delta_parts).strip() or "".join(message_parts).strip()
    return {"choices": [{"message": {"content": content}}]}


def _merge_stream_text(current: str, incoming: str) -> str:
    """Merge streaming text from providers that may send delta or cumulative chunks."""
    if not incoming:
        return current

    if not current:
        return incoming

    if incoming == current:
        return current

    if incoming.startswith(current):
        return incoming

    if current.startswith(incoming):
        return current

    return current + incoming


def _format_model_exception(exc: Exception) -> str:
    """Return useful provider/transport error details without exposing secrets."""
    parts = [f"{exc.__class__.__name__}: {exc}"]

    cause = exc.__cause__
    if cause is not None:
        parts.append(f"cause={cause.__class__.__name__}: {cause}")

    context = exc.__context__
    if context is not None and context is not cause:
        parts.append(f"context={context.__class__.__name__}: {context}")

    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        parts.append(f"status_code={status_code}")

    response = getattr(exc, "response", None)
    if response is not None:
        response_text = getattr(response, "text", None)
        if isinstance(response_text, str) and response_text:
            parts.append(f"response={response_text[:500]}")

    body = getattr(exc, "body", None)
    if body:
        parts.append(f"body={str(body)[:500]}")

    return " | ".join(parts)


def _extract_sdk_response_content(response: Any) -> str:
    """Extract assistant text from an OpenAI SDK response object."""
    try:
        choice = response.choices[0]
    except (AttributeError, IndexError, TypeError) as exc:
        raise ModelError(f"Could not read SDK model response content: {response}") from exc

    message = getattr(choice, "message", None)
    if message is None:
        return ""

    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            text = getattr(item, "text", None)
            if isinstance(text, str):
                text_parts.append(text)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                text_parts.append(item["text"])
        return "\n".join(text_parts)

    return str(content)
