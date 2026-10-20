"""Create Bedrock Prompt Management versions and track local prompt metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError


class PromptNotFound(KeyError):
    """Raised when a prompt version is not present in the local registry."""


@dataclass(frozen=True)
class PromptVersion:
    """Local metadata for a Bedrock Prompt Management version."""

    prompt_id: str
    version_id: str
    description: str | None = None
    content: str | None = None
    registered_at: float = field(default_factory=time.time)

    @property
    def content_hash(self) -> str | None:
        if self.content is None:
            return None
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()[:8]

    def as_dict(self) -> dict:
        return {
            "prompt_id": self.prompt_id,
            "version_id": self.version_id,
            "description": self.description,
            "content_hash": self.content_hash,
            "registered_at": self.registered_at,
        }


class PromptRegistry:
    """Small in-memory registry for prompt version metadata."""

    def __init__(self) -> None:
        self._prompts: dict[tuple[str, str], PromptVersion] = {}

    def register(
        self,
        *,
        prompt_id: str,
        version_id: str,
        content: str | None = None,
        description: str | None = None,
    ) -> PromptVersion:
        prompt_version = PromptVersion(
            prompt_id=prompt_id,
            version_id=version_id,
            description=description,
            content=content,
        )
        self._prompts[(prompt_id, version_id)] = prompt_version
        return prompt_version

    def get(self, *, prompt_id: str, version_id: str) -> PromptVersion:
        try:
            return self._prompts[(prompt_id, version_id)]
        except KeyError as exc:
            raise PromptNotFound(f"{prompt_id}:{version_id}") from exc

    def all_versions(self) -> list[dict]:
        return [prompt_version.as_dict() for prompt_version in self._prompts.values()]


@dataclass(frozen=True)
class TemplatePromptVersion:
    """Versioned prompt template with reproducible template and render hashes."""

    name: str
    version: str
    template: str

    @property
    def template_hash(self) -> str:
        return hashlib.sha256(self.template.encode("utf-8")).hexdigest()[:8]

    def render(self, **kwargs) -> str:
        return self.template.format(**kwargs)

    def render_hash(self, **kwargs) -> str:
        rendered = self.render(**kwargs)
        return hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:8]


def prompt_content_hash(content: str) -> str:
    """Return the full SHA-256 hash for prompt content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def read_prompt_file(prompt_path: str | Path) -> str:
    """Read a local prompt file exactly as the AWS draft should store it."""
    return Path(prompt_path).read_text(encoding="utf-8")


def build_chat_prompt_variant(
    *,
    system_prompt: str,
    variant_name: str = "default",
    model_id: str | None = None,
    user_input_variable: str = "user_input",
    inference_config: dict[str, Any] | None = None,
    additional_model_request_fields: dict[str, Any] | None = None,
    tool_configuration: dict[str, Any] | None = None,
    cache_system_prompt: bool = False,
    metadata: dict[str, str] | None = None,
) -> dict:
    """Build one Bedrock Prompt Management CHAT variant from system prompt text."""
    system_blocks: list[dict[str, Any]] = [{"text": system_prompt}]
    if cache_system_prompt:
        # SystemContentBlock is a tagged union: cachePoint must be its own block.
        system_blocks.append({"cachePoint": {"type": "default"}})

    chat_configuration: dict[str, Any] = {
        "system": system_blocks,
        "messages": [
            {
                "role": "user",
                "content": [{"text": f"User request:\n{{{{{user_input_variable}}}}}"}],
            }
        ],
        "inputVariables": [{"name": user_input_variable}],
    }
    if tool_configuration:
        chat_configuration["toolConfiguration"] = tool_configuration

    variant: dict[str, Any] = {
        "name": variant_name,
        "templateType": "CHAT",
        "templateConfiguration": {
            "chat": chat_configuration,
        },
    }
    if model_id:
        variant["modelId"] = model_id
    if inference_config:
        variant["inferenceConfiguration"] = {"text": inference_config}
    if additional_model_request_fields:
        variant["additionalModelRequestFields"] = additional_model_request_fields
    if metadata:
        variant["metadata"] = [
            {"key": key, "value": value} for key, value in metadata.items() if value is not None
        ]
    return variant


def _system_prompt_from_response(response: dict) -> str | None:
    """Extract the first chat system prompt from a Bedrock Prompt Management response."""
    variants = response.get("variants") or []
    if not variants:
        return None
    chat = variants[0].get("templateConfiguration", {}).get("chat", {})
    system_blocks = chat.get("system") or []
    if not system_blocks:
        return None
    text = system_blocks[0].get("text")
    return text if isinstance(text, str) else None


def build_inference_config(
    *,
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    stop_sequences: list[str] | None = None,
) -> dict[str, Any] | None:
    """Build the Bedrock Prompt Management text inference config."""
    config: dict[str, Any] = {}
    if temperature is not None:
        config["temperature"] = temperature
    if top_p is not None:
        config["topP"] = top_p
    if max_tokens is not None:
        config["maxTokens"] = max_tokens
    if stop_sequences:
        config["stopSequences"] = stop_sequences
    return config or None


def load_json_object(value: str | None) -> dict[str, Any] | None:
    """Load a JSON object from an inline string or a file path."""
    if not value:
        return None
    path = Path(value)
    payload = path.read_text(encoding="utf-8") if path.exists() else value
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("Expected a JSON object")
    return parsed


def _json_schema_property(type_: str, description: str) -> dict[str, str]:
    return {
        "type": type_,
        "description": description,
    }


def build_open_meteo_tool_configuration(*, cache_tools: bool = False) -> dict[str, Any]:
    """Build Prompt Management toolConfiguration for the Open-Meteo tools."""
    tools: list[dict[str, Any]] = [
        {
            "toolSpec": {
                "name": "getForecast",
                "description": (
                    "Get current weather or forecast from Open-Meteo for latitude and longitude."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "latitude": _json_schema_property(
                                "number",
                                "Latitude of the location.",
                            ),
                            "longitude": _json_schema_property(
                                "number",
                                "Longitude of the location.",
                            ),
                            "current_weather": _json_schema_property(
                                "boolean",
                                "Whether to include current weather.",
                            ),
                            "forecast_days": _json_schema_property(
                                "integer",
                                "Number of forecast days to request.",
                            ),
                            "timezone": _json_schema_property(
                                "string",
                                "Timezone for returned local times, for example auto.",
                            ),
                            "daily": _json_schema_property(
                                "string",
                                "Comma-separated daily forecast fields.",
                            ),
                            "hourly": _json_schema_property(
                                "string",
                                "Comma-separated hourly forecast fields.",
                            ),
                        },
                        "required": ["latitude", "longitude"],
                    }
                },
            }
        },
        {
            "toolSpec": {
                "name": "searchLocation",
                "description": (
                    "Resolve a city or place name to coordinates using Open-Meteo geocoding."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "name": _json_schema_property(
                                "string",
                                "City or place name to search for.",
                            ),
                            "count": _json_schema_property(
                                "integer",
                                "Maximum number of matching locations.",
                            ),
                            "language": _json_schema_property(
                                "string",
                                "Language code for the geocoding result.",
                            ),
                        },
                        "required": ["name"],
                    }
                },
            }
        },
    ]
    if cache_tools:
        # Tool is a tagged union too: cachePoint must be a separate list item.
        tools.append({"cachePoint": {"type": "default"}})

    return {
        "tools": tools,
        "toolChoice": {"auto": {}},
    }


def create_or_update_prompt_draft_from_file(
    *,
    region: str,
    prompt_name: str,
    prompt_path: str | Path,
    prompt_id: str | None = None,
    description: str | None = None,
    model_id: str | None = None,
    variant_name: str = "default",
    inference_config: dict[str, Any] | None = None,
    additional_model_request_fields: dict[str, Any] | None = None,
    tool_configuration: dict[str, Any] | None = None,
    cache_system_prompt: bool = False,
    client_token: str | None = None,
) -> dict:
    """Create or update a Bedrock Prompt Management draft from a local system prompt."""
    client = boto3.client("bedrock-agent", region_name=region)
    path = Path(prompt_path)
    content = read_prompt_file(path)
    content_sha256 = prompt_content_hash(content)
    variant = build_chat_prompt_variant(
        system_prompt=content,
        variant_name=variant_name,
        model_id=model_id,
        inference_config=inference_config,
        additional_model_request_fields=additional_model_request_fields,
        tool_configuration=tool_configuration,
        cache_system_prompt=cache_system_prompt,
        metadata={
            "source_path": str(path),
            "prompt_sha256": content_sha256,
        },
    )
    kwargs: dict[str, Any] = {
        "name": prompt_name,
        "description": description or f"Imported from {path}",
        "defaultVariant": variant_name,
        "variants": [variant],
    }

    try:
        if prompt_id:
            kwargs["promptIdentifier"] = prompt_id
            response = client.update_prompt(**kwargs)
            action = "updated"
        else:
            if client_token:
                kwargs["clientToken"] = client_token
            response = client.create_prompt(**kwargs)
            action = "created"
    except ClientError as exc:
        error = exc.response.get("Error", {})
        return {
            "ok": False,
            "error": error.get("Message", str(exc)),
            "error_code": error.get("Code"),
        }

    aws_system_prompt = _system_prompt_from_response(response)
    aws_prompt_sha256 = prompt_content_hash(aws_system_prompt) if aws_system_prompt else None
    return {
        "ok": True,
        "action": action,
        "prompt": response,
        "prompt_id": response.get("id") or prompt_id,
        "prompt_arn": response.get("arn"),
        "prompt_sha256": content_sha256,
        "aws_prompt_sha256": aws_prompt_sha256,
        "prompt_hash_match": aws_prompt_sha256 == content_sha256,
    }


def version_prompt_file_in_aws(
    *,
    region: str,
    prompt_name: str,
    prompt_path: str | Path,
    prompt_id: str | None = None,
    description: str | None = None,
    model_id: str | None = None,
    variant_name: str = "default",
    inference_config: dict[str, Any] | None = None,
    additional_model_request_fields: dict[str, Any] | None = None,
    tool_configuration: dict[str, Any] | None = None,
    cache_system_prompt: bool = False,
    client_token: str | None = None,
) -> dict:
    """Create/update a Prompt Management draft from a file, then freeze a version."""
    draft_result = create_or_update_prompt_draft_from_file(
        region=region,
        prompt_name=prompt_name,
        prompt_path=prompt_path,
        prompt_id=prompt_id,
        description=description,
        model_id=model_id,
        variant_name=variant_name,
        inference_config=inference_config,
        additional_model_request_fields=additional_model_request_fields,
        tool_configuration=tool_configuration,
        cache_system_prompt=cache_system_prompt,
        client_token=client_token,
    )
    if not draft_result.get("ok"):
        return draft_result

    created_prompt_id = draft_result["prompt_id"]
    version_result = create_prompt_version(
        region=region,
        prompt_id=created_prompt_id,
        description=description,
        client_token=client_token,
    )
    if not version_result.get("ok"):
        return {
            **version_result,
            "draft": draft_result,
        }

    version = version_result["prompt_version"]
    aws_system_prompt = _system_prompt_from_response(version)
    aws_prompt_sha256 = prompt_content_hash(aws_system_prompt) if aws_system_prompt else None
    return {
        "ok": True,
        "draft": draft_result,
        "version": version,
        "prompt_id": created_prompt_id,
        "prompt_arn": draft_result.get("prompt_arn"),
        "version_arn": version.get("arn"),
        "version_id": version.get("version"),
        "prompt_sha256": draft_result["prompt_sha256"],
        "aws_version_prompt_sha256": aws_prompt_sha256,
        "version_prompt_hash_match": aws_prompt_sha256 == draft_result["prompt_sha256"],
    }


def create_prompt_version(
    *,
    region: str,
    prompt_id: str,
    description: str | None = None,
    client_token: str | None = None,
) -> dict:
    """Create a static snapshot of a Prompt Management prompt in the specified region."""
    client = boto3.client("bedrock-agent", region_name=region)
    kwargs: dict = {"promptIdentifier": prompt_id}
    if description:
        kwargs["description"] = description
    if client_token:
        kwargs["clientToken"] = client_token

    try:
        response = client.create_prompt_version(**kwargs)
    except ClientError as exc:
        error = exc.response.get("Error", {})
        message = error.get("Message", str(exc))
        return {
            "ok": False,
            "error": message,
            "error_code": error.get("Code"),
        }

    return {
        "ok": True,
        "prompt_version": response,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create or update a Bedrock Prompt Management draft from a local "
            "system prompt, then optionally create an immutable prompt version."
        )
    )
    parser.add_argument("--region", required=True, help="AWS region")
    parser.add_argument("--prompt-name", required=True, help="Prompt name")
    parser.add_argument("--prompt-path", required=True, help="Path to the prompt file")
    parser.add_argument("--prompt-id", help="Prompt ID or ARN when updating an existing prompt")
    parser.add_argument("--description", help="Description for the prompt/version")
    parser.add_argument("--model-id", help="Model ID for the prompt variant")
    parser.add_argument("--variant-name", default="default", help="Variant name for the prompt")
    parser.add_argument("--temperature", type=float, help="Model temperature")
    parser.add_argument("--top-p", type=float, help="Model top-p")
    parser.add_argument("--max-tokens", type=int, help="Maximum output tokens")
    parser.add_argument(
        "--stop-sequence",
        action="append",
        dest="stop_sequences",
        help="Stop sequence; repeat this flag to set multiple values",
    )
    parser.add_argument(
        "--additional-model-request-fields",
        help="JSON object or path to a JSON file for additionalModelRequestFields",
    )
    parser.add_argument(
        "--tool-configuration",
        help="JSON object or path to a JSON file for chat.toolConfiguration",
    )
    parser.add_argument(
        "--include-open-meteo-tools",
        action="store_true",
        help="Add getForecast and searchLocation tool specs to chat.toolConfiguration",
    )
    parser.add_argument(
        "--cache-tools",
        action="store_true",
        help="Add a Bedrock cachePoint after the built-in Open-Meteo tool specs",
    )
    parser.add_argument(
        "--cache-system-prompt",
        action="store_true",
        help="Add a Bedrock cachePoint after the stable system prompt block",
    )
    parser.add_argument("--client-token", help="Client token for idempotency")
    parser.add_argument(
        "--draft-only",
        action="store_true",
        help="Only create/update the draft; do not create an immutable version",
    )
    return parser.parse_args(argv)


def parser_args() -> argparse.Namespace:
    """Backward-compatible wrapper for the earlier helper name."""
    return parse_args()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    inference_config = build_inference_config(
        temperature=args.temperature,
        top_p=args.top_p,
        max_tokens=args.max_tokens,
        stop_sequences=args.stop_sequences,
    )
    additional_model_request_fields = load_json_object(args.additional_model_request_fields)
    tool_configuration = load_json_object(args.tool_configuration)
    if args.include_open_meteo_tools:
        if tool_configuration:
            raise ValueError(
                "Use either --tool-configuration or --include-open-meteo-tools, not both"
            )
        tool_configuration = build_open_meteo_tool_configuration(cache_tools=args.cache_tools)
    elif args.cache_tools:
        raise ValueError("--cache-tools requires --include-open-meteo-tools")
    kwargs = {
        "region": args.region,
        "prompt_name": args.prompt_name,
        "prompt_path": args.prompt_path,
        "prompt_id": args.prompt_id,
        "description": args.description,
        "model_id": args.model_id,
        "variant_name": args.variant_name,
        "inference_config": inference_config,
        "additional_model_request_fields": additional_model_request_fields,
        "tool_configuration": tool_configuration,
        "cache_system_prompt": args.cache_system_prompt,
        "client_token": args.client_token,
    }
    if args.draft_only:
        result = create_or_update_prompt_draft_from_file(**kwargs)
    else:
        result = version_prompt_file_in_aws(**kwargs)

    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
