"""Token metric formatting and normalization."""

from __future__ import annotations


def usage_int(usage: dict, key: str) -> int:
    return int(usage.get(key, 0) or 0)


def normalized_usage(usage: dict) -> dict:
    fresh_input = usage_int(usage, "inputTokens")
    cache_read = usage_int(usage, "cacheReadInputTokens")
    cache_write = usage_int(usage, "cacheWriteInputTokens")
    output = usage_int(usage, "outputTokens")
    prompt_total = fresh_input + cache_read + cache_write
    return {
        "fresh_input": fresh_input,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "prompt_total": prompt_total,
        "output": output,
        "total": prompt_total + output,
    }


def usage_metric_lines(usage: dict) -> list[str]:
    normalized = normalized_usage(usage)
    lines = []
    details = []
    if normalized["total"]:
        lines.append(f"Tokens: **{normalized['total']:,}** total")
    if normalized["prompt_total"]:
        details.append(f"**{normalized['prompt_total']:,}** prompt")
    if normalized["fresh_input"]:
        details.append(f"**{normalized['fresh_input']:,}** fresh in")
    if normalized["cache_read"]:
        details.append(f"**{normalized['cache_read']:,}** cache read")
    if normalized["cache_write"]:
        details.append(f"**{normalized['cache_write']:,}** cache write")
    if normalized["output"]:
        details.append(f"**{normalized['output']:,}** out")
    if details:
        lines.append(f"Detail: {' | '.join(details)}")
    return lines
