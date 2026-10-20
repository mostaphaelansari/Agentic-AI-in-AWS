"""AG-UI protocol adapter for the open-source custom agent.

Translates the service's internal SSE event vocabulary (service.py module
docstring) into AG-UI protocol events (https://docs.ag-ui.com), so any AG-UI
client (e.g. CopilotKit) can drive the agent without custom client code. The
internal contract and the existing /invocations endpoint stay unchanged; this
is a second wire format over the same stream.

Mapping:
    reasoning   -> THINKING_START / THINKING_TEXT_MESSAGE_* deltas
    text        -> TEXT_MESSAGE_START / TEXT_MESSAGE_CONTENT deltas
    tool_start  -> TOOL_CALL_START + TOOL_CALL_ARGS (repeat by id = args only)
    tool_end    -> TOOL_CALL_END + TOOL_CALL_RESULT
    error       -> RUN_ERROR
    metrics     -> CustomEvent(name="metrics")
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from ag_ui.core import (
    BaseEvent,
    CustomEvent,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    ThinkingTextMessageContentEvent,
    ThinkingTextMessageEndEvent,
    ThinkingTextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from ag_ui.encoder import EventEncoder

AGUI_MEDIA_TYPE = "text/event-stream"


def encode_error_run(*, thread_id: str, run_id: str, message: str) -> str:
    """Encode a complete rejected run without opening an async stream."""
    encoder = EventEncoder()
    return "".join(
        [
            encoder.encode(RunStartedEvent(thread_id=thread_id, run_id=run_id)),
            encoder.encode(RunErrorEvent(message=message)),
        ]
    )


def extract_prompt(run_input: RunAgentInput) -> str:
    """Return the text of the most recent user message, or an empty string."""
    for message in reversed(run_input.messages):
        if getattr(message, "role", None) != "user":
            continue
        content = message.content
        if isinstance(content, str):
            return content
        # Multimodal content: keep only the text parts.
        parts = [
            getattr(part, "text", "") for part in (content or []) if getattr(part, "text", None)
        ]
        return " ".join(parts)
    return ""


class _Translator:
    """Stateful mapping from internal event dicts to AG-UI event lists.

    Tracks the open text/thinking block so deltas are wrapped in the
    START/END pairs the protocol requires, and deduplicates the intentional
    double tool_start (stream-side first, hook-side with the final input).
    """

    def __init__(self) -> None:
        self._message_id: str | None = None
        self._thinking = False
        self._started_tool_ids: set[str] = set()

    def _close_blocks(self) -> list[BaseEvent]:
        events: list[BaseEvent] = []
        if self._message_id:
            events.append(TextMessageEndEvent(message_id=self._message_id))
            self._message_id = None
        if self._thinking:
            events.append(ThinkingTextMessageEndEvent())
            events.append(ThinkingEndEvent())
            self._thinking = False
        return events

    def translate(self, event: dict[str, Any]) -> list[BaseEvent]:
        kind = event.get("type")

        if kind == "reasoning":
            events: list[BaseEvent] = []
            if self._message_id:
                events.append(TextMessageEndEvent(message_id=self._message_id))
                self._message_id = None
            if not self._thinking:
                events.append(ThinkingStartEvent())
                events.append(ThinkingTextMessageStartEvent())
                self._thinking = True
            events.append(ThinkingTextMessageContentEvent(delta=event.get("content", "")))
            return events

        if kind == "text":
            events = []
            if self._thinking:
                events.append(ThinkingTextMessageEndEvent())
                events.append(ThinkingEndEvent())
                self._thinking = False
            if not self._message_id:
                self._message_id = f"msg-{uuid.uuid4().hex[:12]}"
                events.append(TextMessageStartEvent(message_id=self._message_id, role="assistant"))
            events.append(
                TextMessageContentEvent(message_id=self._message_id, delta=event.get("content", ""))
            )
            return events

        if kind == "tool_start":
            tool_id = str(event.get("tool_use_id") or f"tool-{uuid.uuid4().hex[:12]}")
            args = json.dumps(event.get("input", {}))
            events = self._close_blocks()
            if tool_id not in self._started_tool_ids:
                self._started_tool_ids.add(tool_id)
                events.append(
                    ToolCallStartEvent(
                        tool_call_id=tool_id,
                        tool_call_name=str(event.get("name", "unknown")),
                    )
                )
            # The hook-side repeat carries the completed input; clients keep
            # the latest args for a tool_call_id.
            events.append(ToolCallArgsEvent(tool_call_id=tool_id, delta=args))
            return events

        if kind == "tool_end":
            tool_id = str(event.get("tool_use_id") or "unknown")
            return [
                *self._close_blocks(),
                ToolCallEndEvent(tool_call_id=tool_id),
                ToolCallResultEvent(
                    message_id=f"msg-{uuid.uuid4().hex[:12]}",
                    tool_call_id=tool_id,
                    content=str(event.get("result", "")),
                ),
            ]

        if kind == "error":
            return [
                *self._close_blocks(),
                RunErrorEvent(message=str(event.get("content", "agent error"))),
            ]

        if kind == "metrics":
            return [CustomEvent(name=kind, value=event)]

        return [CustomEvent(name=str(kind or "unknown"), value=event)]

    def finish(self) -> list[BaseEvent]:
        return self._close_blocks()


def _decode_sse_line(chunk: str) -> dict[str, Any] | None:
    line = chunk.strip()
    if not line.startswith("data:"):
        return None
    try:
        payload = json.loads(line.removeprefix("data:").strip())
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


async def encode_run(
    inner: AsyncIterator[str], *, thread_id: str, run_id: str
) -> AsyncIterator[str]:
    """Wrap the service's internal SSE stream in an AG-UI run.

    ``inner`` yields the existing ``data: {json}`` lines from
    service._event_stream; each is translated and re-encoded as AG-UI SSE.
    An internal ``error`` event maps to RUN_ERROR, after which the protocol
    forbids further events, so the run ends without RUN_FINISHED.
    """
    encoder = EventEncoder()
    translator = _Translator()
    yield encoder.encode(RunStartedEvent(thread_id=thread_id, run_id=run_id))
    errored = False
    async for chunk in inner:
        event = _decode_sse_line(chunk)
        if event is None:
            continue
        for agui_event in translator.translate(event):
            yield encoder.encode(agui_event)
            if isinstance(agui_event, RunErrorEvent):
                errored = True
        if errored:
            return
    for agui_event in translator.finish():
        yield encoder.encode(agui_event)
    yield encoder.encode(RunFinishedEvent(thread_id=thread_id, run_id=run_id))
