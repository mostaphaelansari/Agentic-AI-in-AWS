"""Bidirectional Streamlit component for the shared Mol* structure viewer."""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Any

import streamlit as st
from molecule_viewer import MOLSTAR_CDN

MAX_SELECTED_RESIDUES = 250
MAX_PROMPT_RESIDUES = 32
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.+?-]+$")

MOLSTAR_COMPONENT_HTML = """
<div class="molstar-component-shell" data-molstar-root>
  <div class="molstar-component-host" data-molstar-host></div>
  <div class="molstar-component-loading" data-molstar-loading>Loading Mol*…</div>
  <div class="molstar-component-error" data-molstar-error role="alert"></div>
</div>
"""

MOLSTAR_COMPONENT_CSS = """
.molstar-component-shell {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 320px;
  overflow: hidden;
  background: #101010;
}
.molstar-component-host {
  position: absolute;
  inset: 0;
}
.molstar-component-loading,
.molstar-component-error {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  box-sizing: border-box;
  color: #d0d0d0;
  background: #101010;
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  text-align: center;
}
.molstar-component-error {
  display: none;
  color: #ffb4ab;
}
"""

# Components v2 runs this as an ES module. The Mol* UMD bundle and stylesheet
# are pinned by molecule_viewer.py and loaded once into the Streamlit page.
MOLSTAR_COMPONENT_JS = f"""
const bundleUrl = {MOLSTAR_CDN!r} + '/molstar.js';
const stylesheetUrl = {MOLSTAR_CDN!r} + '/molstar.css';
const scriptPromiseKey = '__awsBlueprintMolstarBundlePromise';
const stylesheetId = 'aws-blueprint-molstar-styles';

function ensureStylesheet() {{
  if (document.getElementById(stylesheetId)) return;
  const link = document.createElement('link');
  link.id = stylesheetId;
  link.rel = 'stylesheet';
  link.href = stylesheetUrl;
  document.head.appendChild(link);
}}

function ensureMolstar() {{
  if (window.molstar) return Promise.resolve(window.molstar);
  if (window[scriptPromiseKey]) return window[scriptPromiseKey];
  window[scriptPromiseKey] = new Promise((resolve, reject) => {{
    const script = document.createElement('script');
    script.src = bundleUrl;
    script.async = true;
    script.onload = () => window.molstar
      ? resolve(window.molstar)
      : reject(new Error('The Mol* bundle loaded without its global API.'));
    script.onerror = () => reject(new Error('The Mol* bundle could not be downloaded.'));
    document.head.appendChild(script);
  }}).catch((error) => {{
    delete window[scriptPromiseKey];
    throw error;
  }});
  return window[scriptPromiseKey];
}}

function asText(value, limit) {{
  if (value === undefined || value === null) return null;
  const text = String(value).trim();
  return text && text.length <= limit ? text : null;
}}

function asInteger(value) {{
  const number = Number(value);
  return Number.isSafeInteger(number) ? number : null;
}}

function selectedResidues(viewer, molstarApi) {{
  const residues = [];
  const seen = new Set();
  let totalResidues = 0;
  const Structure = molstarApi.lib?.structure?.Structure;
  const properties = molstarApi.lib?.structure?.StructureProperties;
  if (!Structure || !properties) return null;

  const entries = viewer.plugin.managers.structure.selection.entries.values();
  for (const entry of entries) {{
    if (!entry.structure) continue;
    Structure.eachAtomicHierarchyElement(entry.structure, {{
      residue: (location) => {{
        const residue = {{
          residue_name: asText(properties.residue.label_comp_id(location), 12),
          label_chain_id: asText(properties.chain.label_asym_id(location), 32),
          auth_chain_id: asText(properties.chain.auth_asym_id(location), 32),
          label_seq_id: asInteger(properties.residue.label_seq_id(location)),
          auth_seq_id: asInteger(properties.residue.auth_seq_id(location)),
          insertion_code: asText(properties.residue.pdbx_PDB_ins_code(location), 8),
        }};
        const fingerprint = JSON.stringify(residue);
        if (seen.has(fingerprint)) return;
        seen.add(fingerprint);
        totalResidues += 1;
        if (residues.length < {MAX_SELECTED_RESIDUES}) residues.push(residue);
      }},
    }});
  }}
  return {{
    residues,
    total_residues: totalResidues,
    truncated: totalResidues > residues.length,
  }};
}}

function publishSelection(state, viewer, molstarApi) {{
  // Mol* applies its built-in click behavior in the same event turn. Reading
  // in a microtask observes the resulting persistent selection manager state.
  queueMicrotask(() => {{
    if (state.disposed) return;
    try {{
      const selection = selectedResidues(viewer, molstarApi);
      if (!selection) return;
      const serialized = JSON.stringify(selection);
      if (serialized === state.lastSelection) return;
      state.lastSelection = serialized;
      state.setStateValue('selection', selection);
    }} catch (error) {{
      console.warn('Could not serialize the Mol* selection.', error);
    }}
  }});
}}

export default async function(component) {{
  const {{ data, parentElement, setStateValue }} = component;
  const root = parentElement.querySelector('[data-molstar-root]');
  const host = parentElement.querySelector('[data-molstar-host]');
  const loading = parentElement.querySelector('[data-molstar-loading]');
  const errorBox = parentElement.querySelector('[data-molstar-error]');
  if (!root || !host || !loading || !errorBox) return;

  root.style.height = `${{data.height}}px`;
  const previous = parentElement.__molstarComponentState;
  if (previous && previous.fingerprint === data.fingerprint && previous.viewer) {{
    previous.setStateValue = setStateValue;
    previous.viewer.handleResize();
    return () => {{
      if (parentElement.__molstarComponentState !== previous) return;
      previous.disposed = true;
      previous.viewer.dispose();
      delete parentElement.__molstarComponentState;
    }};
  }}

  if (previous) {{
    previous.disposed = true;
    previous.viewer?.dispose();
  }}
  host.replaceChildren();
  loading.style.display = 'flex';
  errorBox.style.display = 'none';

  const state = {{
    disposed: false,
    fingerprint: data.fingerprint,
    lastSelection: null,
    setStateValue,
    viewer: null,
  }};
  parentElement.__molstarComponentState = state;

  try {{
    ensureStylesheet();
    const molstarApi = await ensureMolstar();
    if (state.disposed) return;
    const bytes = Uint8Array.from(atob(data.structure_base64), c => c.charCodeAt(0));
    const structure = new TextDecoder('utf-8').decode(bytes);
    const viewer = await molstarApi.Viewer.create(host, data.options);
    state.viewer = viewer;
    if (state.disposed) {{
      viewer.dispose();
      return;
    }}
    await viewer.loadStructureFromData(structure, 'mmcif', data.load_options);
    if (data.selection_mode) viewer.plugin.selectionMode = true;
    if (data.spin && viewer.plugin.canvas3d) {{
      viewer.plugin.canvas3d.setProps({{
        trackball: {{ animate: {{ name: 'spin', params: {{ speed: 1 }} }} }},
      }});
    }}
    viewer.subscribe(viewer.plugin.behaviors.interaction.click, () =>
      publishSelection(state, viewer, molstarApi));
    loading.style.display = 'none';
    viewer.handleResize();
  }} catch (error) {{
    if (state.disposed) return;
    loading.style.display = 'none';
    errorBox.style.display = 'flex';
    errorBox.textContent = 'Mol* could not render this structure: ' + String(error);
  }}

  return () => {{
    if (parentElement.__molstarComponentState !== state) return;
    state.disposed = true;
    state.viewer?.dispose();
    delete parentElement.__molstarComponentState;
  }};
}}
"""

_molstar_component = st.components.v2.component(
    "aws_blueprint_molstar_viewer",
    html=MOLSTAR_COMPONENT_HTML,
    css=MOLSTAR_COMPONENT_CSS,
    js=MOLSTAR_COMPONENT_JS,
    isolate_styles=False,
)


def structure_fingerprint(data: bytes | str) -> str:
    """Return a short stable identifier suitable for component keys."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()[:16]


def _safe_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str) or not (cleaned := value.strip()):
        return None
    if len(cleaned) > limit or not _SAFE_TOKEN.fullmatch(cleaned):
        return None
    return cleaned


def _safe_integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if -10_000_000 <= value <= 10_000_000 else None


def normalize_molstar_selection(value: Any) -> dict[str, Any] | None:
    """Validate untrusted selection state received from the browser component."""
    if not isinstance(value, dict):
        return None
    raw_residues = value.get("residues")
    if not isinstance(raw_residues, list) or len(raw_residues) > MAX_SELECTED_RESIDUES:
        return None

    residues: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for raw in raw_residues:
        if not isinstance(raw, dict):
            return None
        text_fields = {
            "residue_name": (raw.get("residue_name"), 12),
            "label_chain_id": (raw.get("label_chain_id"), 32),
            "auth_chain_id": (raw.get("auth_chain_id"), 32),
            "insertion_code": (raw.get("insertion_code"), 8),
        }
        cleaned_text = {
            name: _safe_text(field_value, limit)
            for name, (field_value, limit) in text_fields.items()
        }
        if any(
            field_value not in (None, "") and cleaned_text[name] is None
            for name, (field_value, _limit) in text_fields.items()
        ):
            return None
        residue = {
            "residue_name": cleaned_text["residue_name"],
            "label_chain_id": cleaned_text["label_chain_id"],
            "auth_chain_id": cleaned_text["auth_chain_id"],
            "label_seq_id": _safe_integer(raw.get("label_seq_id")),
            "auth_seq_id": _safe_integer(raw.get("auth_seq_id")),
            "insertion_code": cleaned_text["insertion_code"],
        }
        if not residue["residue_name"] or not (
            residue["label_chain_id"] or residue["auth_chain_id"]
        ):
            return None
        identity = tuple(residue.values())
        if identity not in seen:
            seen.add(identity)
            residues.append(residue)

    total = value.get("total_residues")
    if isinstance(total, bool) or not isinstance(total, int):
        return None
    if total < len(residues) or total > 10_000_000:
        return None
    return {
        "residues": residues,
        "total_residues": total,
        "truncated": bool(value.get("truncated")) or total > len(residues),
    }


def residue_description(residue: dict[str, Any]) -> str:
    """Format one validated selected residue for UI and prompt context."""
    chain = residue.get("auth_chain_id") or residue.get("label_chain_id") or "?"
    sequence = residue.get("auth_seq_id")
    if sequence is None:
        sequence = residue.get("label_seq_id")
    insertion = residue.get("insertion_code") or ""
    position = f"{chain}:{sequence}{insertion}" if sequence is not None else chain
    return f"{residue['residue_name']} {position}"


def selection_description(selection: dict[str, Any]) -> str:
    """Return a compact human-readable summary of a validated selection."""
    residues = selection["residues"]
    if not residues:
        return "No residues selected"
    preview = ", ".join(residue_description(item) for item in residues[:6])
    remaining = selection["total_residues"] - min(len(residues), 6)
    return f"{preview} (+{remaining} more)" if remaining > 0 else preview


def selection_prompt_context(selection: dict[str, Any]) -> str:
    """Create bounded, non-executable context for the next agent request."""
    residues = selection["residues"][:MAX_PROMPT_RESIDUES]
    if not residues:
        return ""
    rendered = ", ".join(residue_description(item) for item in residues)
    omitted = selection["total_residues"] - len(residues)
    suffix = f"; {omitted} additional selected residues omitted" if omitted > 0 else ""
    return (
        "Mol* UI selection (validated application context): "
        f"{rendered}{suffix}. Treat this as the structure region selected by the user."
    )


def render_molstar_component(
    data: bytes | str,
    *,
    key: str,
    height: int = 640,
    dark: bool = True,
    show_controls: bool = True,
    show_sequence: bool = True,
    spin: bool = False,
    illumination: bool = False,
    color_theme: str | None = None,
) -> dict[str, Any] | None:
    """Render Mol* and return its validated persistent residue selection."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    background = "#101010" if dark else "#FCFBF9"
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
    # Mol* rejects unknown theme names; this allow-list mirrors the validated
    # agent view schema without exposing executable viewer configuration.
    if color_theme in {
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
    }:
        load_options["representationParams"] = {"theme": {"globalName": color_theme}}

    payload = {
        "fingerprint": structure_fingerprint(raw),
        "height": max(320, min(int(height), 1200)),
        "load_options": load_options,
        "options": options,
        "selection_mode": True,
        "spin": bool(spin),
        "structure_base64": base64.b64encode(raw).decode("ascii"),
    }
    result = _molstar_component(
        key=key,
        data=payload,
        default={"selection": None},
        height=payload["height"],
        on_selection_change=lambda: None,
    )
    return normalize_molstar_selection(getattr(result, "selection", None))
