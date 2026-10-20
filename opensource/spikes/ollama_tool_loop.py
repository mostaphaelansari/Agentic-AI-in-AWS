"""Spike: can Strands + a small open model on CPU run our tool loop?

Proves/disproves three things (see docs/opensource-migration-plan.md, Phase 1):
  1. The model actually calls the tool (not just narrates it).
  2. Strands stream events carry enough to emit our existing vocabulary
     (reasoning / text / tool_start / tool_end / metrics).
  3. What the usage payload looks like without Bedrock's cache fields.

Run:  python opensource/spikes/ollama_tool_loop.py [model_id]
"""

import asyncio
import json
import sys
import time

from strands import Agent, tool
from strands.models.ollama import OllamaModel

MODEL_ID = sys.argv[1] if len(sys.argv) > 1 else "qwen3:1.7b"
OLLAMA_HOST = "http://localhost:11434"

tool_invocations: list[dict] = []


@tool
def get_weather(city: str) -> str:
    """Return the current weather for a city."""
    tool_invocations.append({"city": city})
    return f"{city}: 21°C, clear sky, wind 8 km/h"  # stub; real Open-Meteo later


async def main() -> None:
    agent = Agent(
        model=OllamaModel(host=OLLAMA_HOST, model_id=MODEL_ID),
        tools=[get_weather],
        system_prompt="You are a weather assistant. Use your tools to answer.",
        callback_handler=None,  # we consume the stream ourselves
    )

    seen_event_shapes: set[frozenset] = set()
    reasoning_chars = 0
    text_chars = 0
    tool_starts: list[str] = []
    result = None
    start = time.monotonic()

    print(f"=== streaming from {MODEL_ID} (CPU) — first run includes model load ===")
    async for event in agent.stream_async("What's the weather in Casablanca?"):
        keys = frozenset(event.keys())
        if keys not in seen_event_shapes:
            seen_event_shapes.add(keys)
            print(f"\n[event shape] {sorted(event.keys())}")

        # Map onto the app's event vocabulary, same as the real adapter would.
        if "reasoningText" in event:
            reasoning_chars += len(event["reasoningText"] or "")
            print("R", end="", flush=True)  # one mark per reasoning delta
        elif "data" in event:
            text_chars += len(event["data"] or "")
            print(event["data"], end="", flush=True)
        elif "current_tool_use" in event:
            name = event["current_tool_use"].get("name")
            if name and name not in tool_starts:
                tool_starts.append(name)
                print(f"\n>>> tool_start: {name}")
        elif "result" in event:
            result = event["result"]

    elapsed = time.monotonic() - start
    print("\n\n=== spike verdict ===")
    print(f"model:                  {MODEL_ID}")
    print(f"elapsed:                {elapsed:.1f}s")
    print(f"1. tool actually ran:   {bool(tool_invocations)}  {tool_invocations}")
    print(
        f"2. event mapping:       reasoning deltas={reasoning_chars} chars, "
        f"text deltas={text_chars} chars, tool_start events={tool_starts}"
    )
    if result is not None:
        m = result.metrics
        print(f"3. usage payload:       {json.dumps(m.accumulated_usage)}")
        print(f"   cycles:              {m.cycle_count}")
        print(f"   tool metrics:        { {k: v.call_count for k, v in m.tool_metrics.items()} }")
    else:
        print("3. usage payload:       NO RESULT EVENT — investigate")


if __name__ == "__main__":
    asyncio.run(main())
