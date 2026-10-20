"""Split-pane layout rules for the open-source protein workspace."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
UI_PATH = ROOT / "opensource" / "ui" / "app.py"
STYLES_PATH = ROOT / "opensource" / "ui" / "assets" / "styles.css"
sys.path.insert(0, str(ROOT / "opensource" / "agents" / "custom"))

MMCIF = b"data_test\nATOM 1 N N . ALA A 1 1 ? 1.0 1.0 1.0 1.00 0.00 ? 1 ALA A N 1\n"


def _load_ui_module():
    spec = importlib.util.spec_from_file_location("opensource_ui_layout_app", UI_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_molstar_is_only_rendered_with_an_answer() -> None:
    source = UI_PATH.read_text()

    # One call site: render_structure_view. Uploading a file must not render a
    # preview, so no other place in the app may mount the component.
    assert source.count("render_molstar_component(\n") == 1
    assert "3D structure preview" not in source


def test_notebook_is_only_mounted_for_an_answered_structure() -> None:
    source = UI_PATH.read_text()

    assert "notebook_structure: bytes | None = None" in source
    assert "if notebook_structure is not None and (run or selected_history is None):" in source
    assert "render_structure_notebook(notebook_structure)" in source
    assert "structure = uploaded_file.getvalue()" not in source[source.index("elif entry :=") :]


def test_selection_round_trips_through_session_state(monkeypatch) -> None:
    ui = _load_ui_module()
    monkeypatch.setattr(ui, "st", SimpleNamespace(session_state={}))

    assert ui.recalled_structure_selection(MMCIF) is None
    assert ui.recalled_structure_selection(None) is None

    selection = {"chain": "A", "residue_number": 1}
    ui.remember_structure_selection(MMCIF, selection)

    assert ui.recalled_structure_selection(MMCIF) == selection
    assert ui.recalled_structure_selection(b"data_other\n") is None


def test_remembering_nothing_leaves_the_previous_selection(monkeypatch) -> None:
    ui = _load_ui_module()
    monkeypatch.setattr(ui, "st", SimpleNamespace(session_state={}))

    selection = {"chain": "B"}
    ui.remember_structure_selection(MMCIF, selection)
    ui.remember_structure_selection(MMCIF, None)

    assert ui.recalled_structure_selection(MMCIF) == selection


def test_structure_pane_is_pinned_beside_the_scrolling_prompt_pane() -> None:
    styles = STYLES_PATH.read_text()

    assert ".st-key-structure_pane" in styles
    assert ".st-key-prompt_section_split" in styles


def test_the_caption_names_the_theme_actually_applied() -> None:
    ui = _load_ui_module()

    caption = ui.structure_view_caption(
        {
            "caption": "protein · chain A · range A:10–30 · 149 atom records",
            "color_theme": "chain-id",
        }
    )

    # The answer's prose can claim residue-name while the request said chain-id;
    # the caption reports what the viewer was given.
    assert caption.endswith("· colored by chain-id")


def test_the_viewer_key_is_stable_across_view_changes() -> None:
    """A key that tracked the filtered view remounted Mol* on every change.

    Each remount builds a fresh WebGL context while the previous one is still
    being disposed, so repeated view changes — clicking through measurements —
    exhaust the browser's contexts and drop the Streamlit connection. The
    component reloads in place from the fingerprint in its payload, so the key
    must name the uploaded file only.
    """
    source = UI_PATH.read_text()

    assert 'key=(f"opensource_agent_dashboard_{structure_fingerprint(structure)}")' in source
    assert "structure_fingerprint(view['structure'])" not in source
