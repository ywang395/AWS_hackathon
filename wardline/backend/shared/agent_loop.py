"""Bedrock Converse tool-use loop shared by all Wardline agents."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

DEFAULT_MAX_STEPS = 8
DEFAULT_MODEL_MAX_TOKENS = 1024
DEFAULT_REGION = "us-west-2"
SUPPORTED_FINAL_STOP_REASONS = frozenset({"end_turn", "stop_sequence"})

ToolImplementation = Callable[..., Any]


class AgentLoopError(RuntimeError):
    """Base exception for agent-loop failures."""


class AgentConfigurationError(AgentLoopError):
    """Raised when required agent configuration is missing or invalid."""


class AgentStepLimitError(AgentLoopError):
    """Raised when the model requests more steps than the configured limit."""


class BedrockInvocationError(AgentLoopError):
    """Raised when the Bedrock request cannot be completed."""


class BedrockResponseError(AgentLoopError):
    """Raised when Bedrock returns an unusable response."""


class AgentFeedError(AgentLoopError):
    """Raised when an AgentFeed event cannot be recorded."""


@dataclass(frozen=True, slots=True)
class AgentFeedEvent:
    """One auditable agent action destined for the AgentFeed table."""

    ts: str
    agent: str
    action: str
    detail: Mapping[str, Any]


class AgentFeedLogger(Protocol):
    """Interface used by the agent loop to record auditable decisions."""

    def log(self, event: AgentFeedEvent) -> None:
        """Persist or forward one agent feed event."""

        ...


class JsonLoggingAgentFeed:
    """Fallback feed logger that writes structured events to Python logging."""

    def log(self, event: AgentFeedEvent) -> None:
        payload = {
            "ts": event.ts,
            "agent": event.agent,
            "action": event.action,
            "detail": event.detail,
        }
        logger.info(json.dumps(payload, default=str, separators=(",", ":")))


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _emit_feed_event(
    feed_logger: AgentFeedLogger,
    agent_name: str,
    action: str,
    detail: Mapping[str, Any],
) -> None:
    event = AgentFeedEvent(
        ts=_utc_timestamp(),
        agent=agent_name,
        action=action,
        detail=dict(detail),
    )
    try:
        feed_logger.log(event)
    except Exception as exc:
        raise AgentFeedError(
            f"Unable to record AgentFeed event '{action}' for '{agent_name}'."
        ) from exc


def _serialize_tool_result(result: Any) -> str:
    if isinstance(result, str):
        return result

    try:
        return json.dumps(
            result,
            default=str,
            ensure_ascii=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return repr(result)


def _normalize_system_prompt(
    system_prompt: str | Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if isinstance(system_prompt, str):
        return [{"text": system_prompt}]
    return [dict(block) for block in system_prompt]


def _validate_response(response: Any) -> tuple[str, dict[str, Any], list[Any]]:
    if not isinstance(response, Mapping):
        raise BedrockResponseError("Bedrock response must be a mapping.")

    stop_reason = response.get("stopReason")
    if not isinstance(stop_reason, str) or not stop_reason:
        raise BedrockResponseError("Bedrock response is missing stopReason.")

    output = response.get("output")
    if not isinstance(output, Mapping):
        raise BedrockResponseError("Bedrock response is missing output.")

    message = output.get("message")
    if not isinstance(message, Mapping):
        raise BedrockResponseError("Bedrock response is missing output.message.")
    if message.get("role") != "assistant":
        raise BedrockResponseError("Bedrock output message must have assistant role.")

    content = message.get("content")
    if not isinstance(content, list):
        raise BedrockResponseError(
            "Bedrock output message must contain a content list."
        )

    return stop_reason, dict(message), content


def _extract_text(content: Sequence[Any]) -> str:
    parts: list[str] = []
    for block in content:
        if not isinstance(block, Mapping):
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _extract_tool_uses(content: Sequence[Any]) -> list[dict[str, Any]]:
    tool_uses: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, Mapping):
            continue
        tool_use = block.get("toolUse")
        if tool_use is None:
            continue
        if not isinstance(tool_use, Mapping):
            raise BedrockResponseError("Tool-use block must be a mapping.")
        tool_uses.append(dict(tool_use))
    return tool_uses


def _build_tool_result(
    tool_use_id: str,
    result: Any,
    *,
    status: str = "success",
) -> dict[str, Any]:
    return {
        "toolResult": {
            "toolUseId": tool_use_id,
            "content": [{"text": _serialize_tool_result(result)}],
            "status": status,
        }
    }


def _resolve_model_id(model_id: str | None) -> str:
    resolved = model_id or os.getenv("MODEL_ID")
    if not resolved:
        raise AgentConfigurationError(
            "MODEL_ID must be supplied or configured in the environment."
        )
    return resolved


def _create_bedrock_client(region_name: str | None) -> Any:
    resolved_region = (
        region_name
        or os.getenv("BEDROCK_REGION")
        or os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
        or DEFAULT_REGION
    )
    return boto3.client(
        "bedrock-runtime",
        region_name=resolved_region,
        config=Config(
            connect_timeout=3,
            read_timeout=25,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )


def _call_converse(
    bedrock_client: Any,
    request: Mapping[str, Any],
) -> Mapping[str, Any]:
    try:
        response = bedrock_client.converse(**request)
    except (BotoCoreError, ClientError) as exc:
        raise BedrockInvocationError("Amazon Bedrock Converse request failed.") from exc
    except Exception as exc:
        raise BedrockInvocationError("Amazon Bedrock Converse client failed.") from exc

    if not isinstance(response, Mapping):
        raise BedrockResponseError("Bedrock Converse returned a non-mapping response.")
    return response


class AgentLoop:
    """Configurable Converse loop used internally and by unit tests."""

    def __init__(
        self,
        *,
        model_id: str | None = None,
        bedrock_client: Any | None = None,
        feed_logger: AgentFeedLogger | None = None,
        agent_name: str = "agent",
        inference_config: Mapping[str, Any] | None = None,
        region_name: str | None = None,
    ) -> None:
        self._model_id = model_id
        self._bedrock_client = bedrock_client
        self._feed_logger = feed_logger
        self._agent_name = agent_name
        self._inference_config = (
            dict(inference_config) if inference_config is not None else None
        )
        self._region_name = region_name

    def run(
        self,
        system_prompt: str | Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None,
        user_input: str,
        tool_impls: Mapping[str, ToolImplementation],
        max_steps: int = DEFAULT_MAX_STEPS,
    ) -> str:
        """Execute the loop and return the final assistant text response."""

        if max_steps < 1:
            raise AgentConfigurationError("max_steps must be at least 1.")
        if not isinstance(user_input, str):
            raise AgentConfigurationError("user_input must be a string.")

        resolved_model_id = _resolve_model_id(self._model_id)
        active_feed = ( # get the feed logger 
            self._feed_logger
            if self._feed_logger is not None
            else JsonLoggingAgentFeed()
        )
        active_client = ( # client from AWS bedrock 
            self._bedrock_client
            if self._bedrock_client is not None
            else _create_bedrock_client(self._region_name)
        )
        normalized_tools = list(tools or []) # the tools we have
        messages: list[dict[str, Any]] = [ # the initial message 
            {"role": "user", "content": [{"text": user_input}]}
        ]
        active_inference_config = dict( # config for llm 
            self._inference_config
            or {"maxTokens": DEFAULT_MODEL_MAX_TOKENS, "temperature": 0.0}
        )

        _emit_feed_event( # emit feed log 
            active_feed,
            self._agent_name,
            "agent_started",
            {
                "model_id": resolved_model_id,
                "max_steps": max_steps,
                "tool_count": len(normalized_tools),
            },
        )

        for step in range(1, max_steps + 1): # start looping 
            request: dict[str, Any] = {
                "modelId": resolved_model_id,
                "messages": messages,
                "system": _normalize_system_prompt(system_prompt),
                "inferenceConfig": active_inference_config,
            }
            if normalized_tools:
                request["toolConfig"] = {"tools": normalized_tools}

            response = _call_converse(active_client, request) # call api
            stop_reason, _, content = _validate_response(response) # we want output.message.content

            if stop_reason == "tool_use":
                tool_uses = _extract_tool_uses(content) # if the agent wants to call a tool
                if not tool_uses:
                    raise BedrockResponseError(
                        "Bedrock stopped for tool use without a toolUse block."
                    )
                if step == max_steps: # we shouldnt call tool_call at this point -> emphasize at the prompt 
                    requested_tools = [
                        tool_use.get("name") for tool_use in tool_uses
                    ]
                    _emit_feed_event(
                        active_feed,
                        self._agent_name,
                        "step_limit_reached",
                        {
                            "step": step,
                            "max_steps": max_steps,
                            "requested_tools": requested_tools,
                        },
                    )
                    raise AgentStepLimitError(
                        f"Agent exceeded max_steps={max_steps}."
                    )

                messages.append({"role": "assistant", "content": content})
                tool_results: list[dict[str, Any]] = []

                for tool_use in tool_uses:
                    tool_use_id = tool_use.get("toolUseId")
                    tool_name = tool_use.get("name")
                    tool_input = tool_use.get("input", {})

                    if not isinstance(tool_use_id, str) or not tool_use_id:
                        raise BedrockResponseError(
                            "Tool-use block is missing toolUseId."
                        )
                    if not isinstance(tool_name, str) or not tool_name:
                        raise BedrockResponseError("Tool-use block is missing name.")
                    if not isinstance(tool_input, Mapping):
                        raise BedrockResponseError(
                            "Tool-use input must be a mapping."
                        )

                    _emit_feed_event(
                        active_feed,
                        self._agent_name,
                        "tool_requested",
                        {
                            "step": step,
                            "tool_use_id": tool_use_id,
                            "tool": tool_name,
                            "input": dict(tool_input),
                        },
                    )

                    implementation = tool_impls.get(tool_name) # get the tool
                    if implementation is None or not callable(implementation):
                        error_message = f"Tool '{tool_name}' is not available."
                        _emit_feed_event(
                            active_feed,
                            self._agent_name,
                            "tool_failed",
                            {
                                "step": step,
                                "tool_use_id": tool_use_id,
                                "tool": tool_name,
                                "error_type": "UnknownTool",
                                "error": error_message,
                            },
                        )
                        tool_results.append(
                            _build_tool_result(
                                tool_use_id,
                                {"error": error_message},
                                status="error",
                            )
                        )
                        continue

                    try:
                        # execute the tool
                        result = implementation(**dict(tool_input))
                    except Exception as exc:
                        error_message = f"Tool '{tool_name}' failed."
                        _emit_feed_event(
                            active_feed,
                            self._agent_name,
                            "tool_failed",
                            {
                                "step": step,
                                "tool_use_id": tool_use_id,
                                "tool": tool_name,
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                            },
                        )
                        tool_results.append(
                            _build_tool_result(
                                tool_use_id,
                                {
                                    "error": error_message,
                                    "error_type": type(exc).__name__,
                                },
                                status="error",
                            )
                        )
                    else:
                        _emit_feed_event(
                            active_feed,
                            self._agent_name,
                            "tool_completed",
                            {
                                "step": step,
                                "tool_use_id": tool_use_id,
                                "tool": tool_name,
                                "result": result,
                            },
                        )
                        tool_results.append(_build_tool_result(tool_use_id, result))

                messages.append({"role": "user", "content": tool_results})
                continue

            # when agent want to end the conversation 
            if stop_reason in SUPPORTED_FINAL_STOP_REASONS:
                final_text = _extract_text(content)
                if not final_text:
                    raise BedrockResponseError(
                        "Bedrock completed without a text response."
                    )
                _emit_feed_event(
                    active_feed,
                    self._agent_name,
                    "agent_completed",
                    {
                        "steps": step,
                        "stop_reason": stop_reason,
                        "reply": final_text,
                    },
                )
                return final_text

            raise BedrockResponseError(
                f"Unsupported Bedrock stopReason: {stop_reason}."
            )

        raise AgentStepLimitError(f"Agent exceeded max_steps={max_steps}.")


def run_agent(
    system_prompt: str | Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]] | None,
    user_input: str,
    tool_impls: Mapping[str, ToolImplementation],
    max_steps: int = DEFAULT_MAX_STEPS,
) -> str:
    """Run the shared agent loop using environment-configured AWS dependencies.

    Tool implementations are trusted, server-bound callables. They receive only
    the arguments requested by the model and must perform their own input and
    ownership validation.
    """

    return AgentLoop(
        agent_name=os.getenv("AGENT_NAME", "agent")
    ).run(
        system_prompt,
        tools,
        user_input,
        tool_impls,
        max_steps,
    )
