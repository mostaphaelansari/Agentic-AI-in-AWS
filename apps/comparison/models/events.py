"""Normalized backend event types."""

from __future__ import annotations

from typing import Annotated, Any, Literal, TypeAlias, TypedDict

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

EventType = Literal[
    "reasoning",
    "text",
    "tool_start",
    "tool_end",
    "metrics",
    "error",
]


class EventBase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source: str | None = None


class ReasoningEvent(EventBase):
    type: Literal["reasoning"]
    content: str


class TextEvent(EventBase):
    type: Literal["text"]
    content: str


class ToolStartEvent(EventBase):
    type: Literal["tool_start"]
    tool_use_id: str | None = None
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolEndEvent(EventBase):
    type: Literal["tool_end"]
    tool_use_id: str | None = None
    name: str
    result: str = ""
    summary: str | None = None


class MetricsUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    inputTokens: int = 0
    outputTokens: int = 0
    totalTokens: int = 0
    cacheReadInputTokens: int = 0
    cacheWriteInputTokens: int = 0


class MetricsEvent(EventBase):
    type: Literal["metrics"]
    usage: MetricsUsage
    tool_calls: int = 0
    browse_calls: int = 0
    cycles: int = 0
    tool_names: list[str] = Field(default_factory=list)


class ErrorEvent(EventBase):
    type: Literal["error"]
    content: str


AgentEventModel: TypeAlias = Annotated[
    ReasoningEvent | TextEvent | ToolStartEvent | ToolEndEvent | MetricsEvent | ErrorEvent,
    Field(discriminator="type"),
]

AgentEventAdapter = TypeAdapter(AgentEventModel)


def validate_agent_event(event: dict[str, Any]) -> AgentEventModel:
    """Validate a normalized backend event and return its typed model."""
    return AgentEventAdapter.validate_python(event)


class AgentEvent(TypedDict, total=False):
    type: EventType
    source: str
    content: str
    tool_use_id: str
    name: str
    input: dict[str, Any]
    result: str
    summary: str
    usage: dict[str, int]
    tool_calls: int
    browse_calls: int
    cycles: int
    tool_names: list[str]


QueueItem = tuple[str, Any]
