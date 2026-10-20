"""Extract a safe Vega-Lite subset from code-interpreter results."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

VEGA_LITE_MARKER = "__VEGA_LITE__:"
ALLOWED_MARKS = {"bar", "line", "point", "area", "arc"}
ALLOWED_CHANNELS = {"x", "y", "color", "tooltip"}


def _walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _safe_spec(candidate: Any) -> dict[str, Any] | None:
    if not isinstance(candidate, dict):
        return None
    mark = candidate.get("mark")
    mark_name = mark.get("type") if isinstance(mark, dict) else mark
    data = candidate.get("data")
    values = data.get("values") if isinstance(data, dict) else None
    encoding = candidate.get("encoding")
    if mark_name not in ALLOWED_MARKS or not isinstance(values, list):
        return None
    if not values or len(values) > 500 or not isinstance(encoding, dict):
        return None
    if any(not isinstance(row, dict) for row in values):
        return None
    if len(json.dumps(values, ensure_ascii=False, default=str)) > 100_000:
        return None
    clean_encoding = {
        channel: definition
        for channel, definition in encoding.items()
        if channel in ALLOWED_CHANNELS and isinstance(definition, (dict, list, str))
    }
    if not clean_encoding:
        return None
    spec: dict[str, Any] = {
        "data": {"values": values},
        "mark": mark if isinstance(mark, dict) else mark_name,
        "encoding": clean_encoding,
    }
    if isinstance(candidate.get("title"), str):
        spec["title"] = candidate["title"][:160]
    return spec


def _marked_json_values(value: str) -> Iterable[tuple[int, int, Any]]:
    """Yield spans and decoded JSON values following Vega-Lite markers."""
    decoder = json.JSONDecoder()
    start = 0
    while (marker_at := value.find(VEGA_LITE_MARKER, start)) >= 0:
        payload_start = marker_at + len(VEGA_LITE_MARKER)
        whitespace = len(value[payload_start:]) - len(value[payload_start:].lstrip())
        json_start = payload_start + whitespace
        try:
            candidate, consumed = decoder.raw_decode(value[json_start:])
        except json.JSONDecodeError:
            break
        end = json_start + consumed
        yield marker_at, end, candidate
        start = end


def chart_specs_from_result(result: Any) -> list[dict[str, Any]]:
    parsed = result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            parsed = result
    specs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in _walk(parsed):
        if not isinstance(value, str):
            continue
        for _, _, candidate in _marked_json_values(value):
            spec = _safe_spec(candidate)
            if spec is None:
                continue
            fingerprint = json.dumps(spec, sort_keys=True, ensure_ascii=False, default=str)
            if fingerprint not in seen:
                seen.add(fingerprint)
                specs.append(spec)
    return specs


def response_without_chart_payloads(response: str) -> str:
    """Remove decoded machine-readable chart payloads from response prose."""
    spans = [(start, end) for start, end, _ in _marked_json_values(response)]
    if not spans:
        return response.strip()

    chunks: list[str] = []
    cursor = 0
    for start, end in spans:
        chunks.append(response[cursor:start])
        cursor = end
    chunks.append(response[cursor:])
    return "".join(chunks).strip()


def chart_specs_for_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    specs = chart_specs_from_result(state.get("answer", ""))
    for tool in state.get("tools", []):
        normalized_name = str(tool.get("name", "")).rsplit("___", 1)[-1]
        if normalized_name in {"code_interpreter", "python"} and tool.get("result") is not None:
            specs.extend(chart_specs_from_result(tool["result"]))

    unique_specs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for spec in specs:
        fingerprint = json.dumps(spec, sort_keys=True, ensure_ascii=False, default=str)
        if fingerprint not in seen:
            seen.add(fingerprint)
            unique_specs.append(spec)
    return unique_specs
