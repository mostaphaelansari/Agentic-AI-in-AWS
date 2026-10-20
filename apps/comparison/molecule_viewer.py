"""Shared Mol* HTML generation for uploaded mmCIF structure previews."""

from __future__ import annotations

import base64
import json
import shlex
from collections.abc import Iterable
from typing import Any

MOLSTAR_VERSION = "5.11.0"
MOLSTAR_CDN = f"https://cdn.jsdelivr.net/npm/molstar@{MOLSTAR_VERSION}/build/viewer"
MOLSTAR_VIEW_MARKER = "__MOLSTAR_VIEW__:"

_ALLOWED_INCLUDES = {"all", "protein", "polymer", "ligands"}
_ALLOWED_MODES = {"replace", "add"}
# Bounds how many additive turns can stack onto one view; each layer costs a
# full pass over every atom-site row.
_MAX_VIEW_LAYERS = 8
_ALLOWED_COLOR_THEMES = {
    "chain-id",
    "element-symbol",
    "entity-id",
    "model-index",
    "operator-name",
    "polymer-id",
    "residue-name",
    "secondary-structure",
    "sequence-id",
    "uniform",
}
_WATER_NAMES = {"DOD", "HOH", "WAT"}
# Common amino-acid residue codes, including frequently encountered modified
# polymer residues. This is deliberately a data allow-list, not executable Mol*
# selection syntax supplied by the model.
_PROTEIN_RESIDUES = {
    "ALA",
    "ARG",
    "ASN",
    "ASP",
    "ASX",
    "CYS",
    "GLN",
    "GLU",
    "GLX",
    "GLY",
    "HIS",
    "HYP",
    "ILE",
    "LEU",
    "LYS",
    "MET",
    "MSE",
    "PHE",
    "PRO",
    "PYL",
    "SEC",
    "SER",
    "THR",
    "TRP",
    "TYR",
    "UNK",
    "VAL",
}


def atom_record_count(data: bytes | str) -> int:
    """Count PDBx/mmCIF atom-site rows in RCSB-style structure text."""
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    return sum(1 for line in text.splitlines() if line.startswith(("ATOM ", "HETATM ")))


def _walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _marked_json_values(value: str) -> Iterable[tuple[int, int, Any]]:
    decoder = json.JSONDecoder()
    cursor = 0
    while (marker_at := value.find(MOLSTAR_VIEW_MARKER, cursor)) >= 0:
        payload_start = marker_at + len(MOLSTAR_VIEW_MARKER)
        json_start = (
            payload_start + len(value[payload_start:]) - len(value[payload_start:].lstrip())
        )
        try:
            candidate, consumed = decoder.raw_decode(value[json_start:])
        except json.JSONDecodeError:
            cursor = payload_start
            continue
        end = json_start + consumed
        yield marker_at, end, candidate
        cursor = end


def _safe_strings(value: Any, *, upper: bool = False) -> list[str] | None:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 32:
        return None
    cleaned: list[str] = []
    for item in value:
        if not isinstance(item, str) or not (token := item.strip()) or len(token) > 12:
            return None
        cleaned.append(token.upper() if upper else token)
    return list(dict.fromkeys(cleaned))


def _safe_view_spec(candidate: Any) -> dict[str, Any] | None:
    """Validate the small declarative selection language accepted from an agent."""
    if not isinstance(candidate, dict):
        return None
    allowed_keys = {
        "include",
        "chains",
        "residue_names",
        "residue_ranges",
        "exclude_water",
        "color_theme",
        "mode",
    }
    if set(candidate) - allowed_keys:
        return None
    include = str(candidate.get("include", "all")).lower()
    if include not in _ALLOWED_INCLUDES:
        return None
    chains = _safe_strings(candidate.get("chains"))
    residue_names = _safe_strings(candidate.get("residue_names"), upper=True)
    if chains is None or residue_names is None:
        return None

    raw_ranges = candidate.get("residue_ranges", [])
    if not isinstance(raw_ranges, list) or len(raw_ranges) > 32:
        return None
    ranges: list[dict[str, Any]] = []
    for item in raw_ranges:
        if not isinstance(item, dict):
            return None
        start, end = item.get("start"), item.get("end")
        chain = item.get("chain")
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
            or start < -100_000
            or end > 100_000
            or start > end
            or (chain is not None and (not isinstance(chain, str) or not chain.strip()))
        ):
            return None
        safe_range: dict[str, Any] = {"start": start, "end": end}
        if chain is not None:
            safe_range["chain"] = chain.strip()[:12]
        ranges.append(safe_range)

    exclude_water = candidate.get("exclude_water", True)
    if not isinstance(exclude_water, bool):
        return None
    color_theme = candidate.get("color_theme", "chain-id")
    if color_theme not in _ALLOWED_COLOR_THEMES:
        return None
    # "add" keeps whatever the previous turn displayed and unions this spec on
    # top of it; "replace" (the default) starts a new view from scratch.
    mode = str(candidate.get("mode", "replace")).lower()
    if mode not in _ALLOWED_MODES:
        return None

    return {
        "include": include,
        "chains": chains,
        "residue_names": residue_names,
        "residue_ranges": ranges,
        "exclude_water": exclude_water,
        "color_theme": color_theme,
        "mode": mode,
    }


def normalize_view_spec(candidate: Any) -> dict[str, Any] | None:
    """Return the validated, default-filled declarative Mol* view request.

    This public wrapper lets trusted UI surfaces such as the structure notebook
    use exactly the same data-only protocol as agent responses without exposing
    executable Mol* or JavaScript APIs.
    """
    return _safe_view_spec(candidate)


def view_specs_from_result(result: Any) -> list[dict[str, Any]]:
    """Extract unique, validated Mol* view requests from nested agent output."""
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
            spec = _safe_view_spec(candidate)
            if spec is None:
                continue
            fingerprint = json.dumps(spec, sort_keys=True)
            if fingerprint not in seen:
                seen.add(fingerprint)
                specs.append(spec)
    return specs


def view_specs_for_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    specs = view_specs_from_result(state.get("answer", ""))
    for tool in state.get("tools", []):
        normalized_name = str(tool.get("name", "")).rsplit("___", 1)[-1]
        if normalized_name in {"code_interpreter", "python"} and tool.get("result") is not None:
            specs.extend(view_specs_from_result(tool["result"]))
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for spec in specs:
        fingerprint = json.dumps(spec, sort_keys=True)
        if fingerprint not in seen:
            seen.add(fingerprint)
            unique.append(spec)
    return unique


def response_without_view_payloads(response: str) -> str:
    """Remove valid machine-readable Mol* requests from user-facing prose."""
    spans = [
        (start, end)
        for start, end, value in _marked_json_values(response)
        if _safe_view_spec(value)
    ]
    if not spans:
        return response.strip()
    chunks: list[str] = []
    cursor = 0
    for start, end in spans:
        chunks.append(response[cursor:start])
        cursor = end
    chunks.append(response[cursor:])
    return "".join(chunks).strip()


def _atom_value(row: dict[str, str], *names: str) -> str:
    for name in names:
        value = row.get(name, "")
        if value not in {"", ".", "?"}:
            return value
    return ""


def _integer_residue(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _row_matches(row: dict[str, str], spec: dict[str, Any]) -> bool:
    group = _atom_value(row, "group_PDB").upper()
    residue = _atom_value(row, "auth_comp_id", "label_comp_id").upper()
    chains = {
        value
        for value in (
            _atom_value(row, "auth_asym_id"),
            _atom_value(row, "label_asym_id"),
        )
        if value
    }

    if spec["exclude_water"] and residue in _WATER_NAMES:
        return False
    include = spec["include"]
    if include == "protein" and residue not in _PROTEIN_RESIDUES:
        return False
    if include == "polymer" and group != "ATOM":
        return False
    if include == "ligands" and (group != "HETATM" or residue in _PROTEIN_RESIDUES):
        return False
    if spec["chains"] and chains.isdisjoint(spec["chains"]):
        return False
    if spec["residue_names"] and residue not in spec["residue_names"]:
        return False

    if spec["residue_ranges"]:
        author_chain = _atom_value(row, "auth_asym_id")
        label_chain = _atom_value(row, "label_asym_id")
        author_seq = _integer_residue(_atom_value(row, "auth_seq_id"))
        label_seq = _integer_residue(_atom_value(row, "label_seq_id"))

        def range_matches(item: dict[str, Any]) -> bool:
            requested_chain = item.get("chain")
            sequence_ids: list[int] = []
            if not requested_chain or requested_chain == author_chain:
                if author_seq is not None:
                    sequence_ids.append(author_seq)
            if not requested_chain or requested_chain == label_chain:
                if label_seq is not None:
                    sequence_ids.append(label_seq)
            return any(item["start"] <= sequence_id <= item["end"] for sequence_id in sequence_ids)

        if not any(range_matches(item) for item in spec["residue_ranges"]):
            return False
    return True


def _safe_view_layers(spec: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Validate one spec or a stack of them, keeping order."""
    candidates = spec if isinstance(spec, list) else [spec]
    if not candidates or len(candidates) > _MAX_VIEW_LAYERS:
        return None
    layers: list[dict[str, Any]] = []
    for candidate in candidates:
        safe = _safe_view_spec(candidate)
        if safe is None:
            return None
        layers.append(safe)
    return layers


def filter_mmcif(
    data: bytes | str,
    spec: dict[str, Any] | list[dict[str, Any]],
) -> tuple[bytes, int]:
    """Return an mmCIF containing only atom-site rows selected by safe specs.

    A stack of specs is a union: criteria are ANDed inside one spec and ORed
    across them, so "residues 10-30" plus "the iron" keeps both.
    """
    safe_layers = _safe_view_layers(spec)
    if safe_layers is None:
        raise ValueError("The requested structure view is invalid.")
    text = data.decode("utf-8-sig") if isinstance(data, bytes) else data.lstrip("\ufeff")
    lines = text.splitlines(keepends=True)

    index = 0
    while index < len(lines):
        if lines[index].strip().lower() != "loop_":
            index += 1
            continue
        header_end = index + 1
        headers: list[str] = []
        while header_end < len(lines) and lines[header_end].lstrip().startswith("_"):
            headers.append(lines[header_end].strip().split()[0])
            header_end += 1
        if not headers or not all(header.startswith("_atom_site.") for header in headers):
            index = max(header_end, index + 1)
            continue

        names = [header.split(".", 1)[1] for header in headers]
        kept_lines: list[str] = []
        row_lines: list[str] = []
        matched = 0
        cursor = header_end
        while cursor < len(lines):
            stripped = lines[cursor].strip()
            if not row_lines and (
                stripped == "#"
                or stripped.lower() == "loop_"
                or stripped.lower().startswith(("data_", "save_"))
                or stripped.startswith("_")
            ):
                break
            if not stripped and not row_lines:
                kept_lines.append(lines[cursor])
                cursor += 1
                continue
            if stripped.startswith(";"):
                raise ValueError("Multiline atom-site values are not supported for filtered views.")
            row_lines.append(lines[cursor])
            try:
                values = shlex.split(" ".join(line.strip() for line in row_lines), posix=True)
            except ValueError as exc:
                raise ValueError("The atom-site table could not be parsed safely.") from exc
            if len(values) < len(names):
                cursor += 1
                continue
            if len(values) > len(names):
                raise ValueError("The atom-site table has an unsupported row layout.")
            row = dict(zip(names, values))
            if any(_row_matches(row, layer) for layer in safe_layers):
                kept_lines.extend(row_lines)
                matched += 1
            row_lines = []
            cursor += 1
        if row_lines:
            raise ValueError("The atom-site table ended with an incomplete row.")
        if matched == 0:
            raise ValueError("No atoms matched the requested structure view.")
        filtered = "".join(lines[:header_end] + kept_lines + lines[cursor:])
        return filtered.encode("utf-8"), matched
    raise ValueError("No `_atom_site` loop was found in the uploaded mmCIF file.")


def view_description(spec: dict[str, Any]) -> str:
    parts = [spec["include"]]
    if spec["chains"]:
        parts.append("chain " + ", ".join(spec["chains"]))
    if spec["residue_names"]:
        parts.append("residues " + ", ".join(spec["residue_names"]))
    if spec["residue_ranges"]:
        ranges = [
            f"{item.get('chain') + ':' if item.get('chain') else ''}{item['start']}–{item['end']}"
            for item in spec["residue_ranges"]
        ]
        parts.append("range " + ", ".join(ranges))
    if not spec["exclude_water"]:
        parts.append("including water")
    return " · ".join(parts)


def layers_description(specs: list[dict[str, Any]]) -> str:
    """Describe a stack of view layers as the union it represents."""
    return " + ".join(view_description(spec) for spec in specs)


def compose_view_layers(
    previous: list[dict[str, Any]] | None,
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    """Fold this turn's view requests onto what the viewer already shows.

    A `replace` spec restarts the stack; an `add` spec unions onto it. A turn
    that requests no view at all leaves the current view standing, so ordinary
    follow-up questions do not wipe the structure on screen.
    """
    layers = list(previous or [])
    for spec in view_specs_for_state(state):
        if spec["mode"] == "replace" or not layers:
            layers = [spec]
            continue
        if spec not in layers:
            layers.append(spec)
    return layers[-_MAX_VIEW_LAYERS:]


def agent_structure_view(
    data: bytes | str,
    state: dict[str, Any],
    *,
    layers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve an agent-requested view, falling back to the full structure.

    `layers` is a pre-composed stack (see `compose_view_layers`); without it
    only this turn's first view request is honored.
    """
    original = data.encode("utf-8") if isinstance(data, str) else data
    resolved = {
        "structure": original,
        "caption": f"Full uploaded structure · {atom_record_count(original):,} atom records",
        "color_theme": "chain-id",
        "agent_directed": False,
        "warning": None,
    }
    specs = layers if layers is not None else view_specs_for_state(state)[:1]
    if not specs:
        return resolved

    try:
        filtered, matched = filter_mmcif(original, specs)
    except ValueError as exc:
        resolved["warning"] = (
            f"{exc} Requested criteria: {layers_description(specs)}. "
            "Showing the full uploaded structure instead."
        )
        return resolved

    resolved.update(
        {
            "structure": filtered,
            "caption": f"{layers_description(specs)} · {matched:,} atom records",
            # The most recent layer owns the theme; earlier ones only add atoms.
            "color_theme": specs[-1]["color_theme"],
            "agent_directed": True,
        }
    )
    return resolved


def molstar_html(
    data: bytes | str,
    *,
    dark: bool = True,
    show_controls: bool = True,
    show_sequence: bool = True,
    spin: bool = False,
    illumination: bool = False,
    color_theme: str | None = None,
) -> str:
    """Return a standalone Mol* viewer page with mmCIF bytes embedded.

    Base64 keeps arbitrary structure text out of JavaScript string-literal and
    HTML parsing contexts. Mol* itself is pinned and loaded from jsDelivr.
    """
    raw = data.encode("utf-8") if isinstance(data, str) else data
    payload = base64.b64encode(raw).decode("ascii")
    background = "#101010" if dark else "#FCFBF9"
    css_class = "msp-theme-dark" if dark else ""
    options = {
        "layoutIsExpanded": False,
        "layoutShowControls": show_controls,
        "layoutShowRemoteState": False,
        "layoutShowSequence": show_sequence,
        "layoutShowLog": False,
        "layoutShowLeftPanel": show_controls,
        "collapseLeftPanel": False,
        "viewportShowExpand": True,
        "viewportShowControls": True,
        "viewportShowSettings": True,
        "viewportShowSelectionMode": True,
        "viewportShowAnimation": True,
        "viewportShowScreenshotControls": True,
        "viewportShowTrajectoryControls": True,
        "viewportBackgroundColor": background,
        "illumination": illumination,
        "pdbProvider": "rcsb",
        "emdbProvider": "rcsb",
    }

    load_options: dict[str, Any] = {}
    if color_theme in _ALLOWED_COLOR_THEMES:
        load_options["representationParams"] = {"theme": {"globalName": color_theme}}

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<link rel="stylesheet" type="text/css" href="{MOLSTAR_CDN}/molstar.css" />
<style>
  html, body {{ margin: 0; padding: 0; overflow: hidden; background: {background}; }}
  #app {{ position: absolute; inset: 0; }}
  #fallback {{ display: none; position: absolute; inset: 0; padding: 24px;
               box-sizing: border-box; font: 14px/1.5 -apple-system, "Segoe UI", sans-serif;
               color: #d0d0d0; background: #101010; }}
</style>
</head>
<body class="{css_class}">
<div id="app"></div>
<div id="fallback">
  <b>Mol* could not be loaded.</b><br>
  The viewer bundle is served from jsDelivr, so your browser needs internet access.
</div>
<script type="text/javascript" src="{MOLSTAR_CDN}/molstar.js"></script>
<script type="text/javascript">
  const showFailure = (message) => {{
    const el = document.getElementById('fallback');
    el.style.display = 'block';
    if (message) el.appendChild(document.createTextNode(' ' + message));
  }};
  if (typeof molstar === 'undefined') {{
    showFailure();
  }} else {{
    const bytes = Uint8Array.from(atob("{payload}"), c => c.charCodeAt(0));
    const data = new TextDecoder('utf-8').decode(bytes);
    molstar.Viewer.create('app', {json.dumps(options)}).then(async viewer => {{
      await viewer.loadStructureFromData(data, 'mmcif', {json.dumps(load_options)});
      if ({json.dumps(spin)} && viewer.plugin.canvas3d) {{
        viewer.plugin.canvas3d.setProps({{
          trackball: {{ animate: {{ name: 'spin', params: {{ speed: 1 }} }} }}
        }});
      }}
      window.addEventListener('resize', () => viewer.handleResize());
    }}).catch(err => showFailure('The uploaded structure could not be rendered: ' + err));
  }}
</script>
</body>
</html>"""
