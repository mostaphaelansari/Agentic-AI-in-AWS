from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path

COMPARISON_APP = Path(__file__).resolve().parents[2] / "apps" / "comparison"
sys.path.insert(0, str(COMPARISON_APP))

from app import prompt_controlled_view_slots  # noqa: E402
from molecule_viewer import (  # noqa: E402
    MOLSTAR_CDN,
    MOLSTAR_VERSION,
    agent_structure_view,
    atom_record_count,
    compose_view_layers,
    filter_mmcif,
    molstar_html,
    response_without_view_payloads,
    view_description,
    view_specs_for_state,
)

MMCIF = """data_demo
loop_
_atom_site.group_PDB
_atom_site.id
_atom_site.type_symbol
_atom_site.label_atom_id
_atom_site.label_comp_id
_atom_site.label_asym_id
_atom_site.label_seq_id
_atom_site.auth_comp_id
_atom_site.auth_asym_id
_atom_site.auth_seq_id
ATOM 1 C CA ALA A 10 ALA X 110
ATOM 2 N N GLY A 11 GLY X 111
ATOM 3 P P DA B 4 DA Y 204
HETATM 4 C C1 ATP C . ATP Z 501
HETATM 5 O O HOH D . HOH W 900
#
"""


def test_molstar_html_embeds_mmcif_as_base64_not_raw_markup() -> None:
    structure = b"data_demo\n# </script><script>not executable</script>\nATOM 1\nHETATM 2\n"

    rendered = molstar_html(structure)

    payload = re.search(r'atob\("([A-Za-z0-9+/=]+)"\)', rendered)
    assert payload is not None
    assert base64.b64decode(payload.group(1)) == structure
    assert "</script><script>not executable</script>" not in rendered
    assert "new TextDecoder('utf-8')" in rendered
    assert "loadStructureFromData(data, 'mmcif', {})" in rendered


def test_molstar_bundle_is_pinned_and_failure_is_visible() -> None:
    rendered = molstar_html("data_demo\n")

    assert MOLSTAR_VERSION == "5.11.0"
    assert f"{MOLSTAR_CDN}/molstar.css" in rendered
    assert f"{MOLSTAR_CDN}/molstar.js" in rendered
    assert "Mol* could not be loaded" in rendered
    assert "showFailure" in rendered


def test_atom_record_count_handles_bytes_and_text() -> None:
    structure = "data_demo\nATOM 1\nATOM 2\nHETATM 3\n#\n"

    assert atom_record_count(structure) == 3
    assert atom_record_count(structure.encode()) == 3


def test_view_spec_is_extracted_from_nested_tool_output_and_answer_is_cleaned() -> None:
    candidate = {
        "include": "protein",
        "chains": ["X"],
        "residue_names": [],
        "residue_ranges": [{"chain": "X", "start": 110, "end": 111}],
        "exclude_water": True,
        "color_theme": "residue-name",
    }
    marker = f"__MOLSTAR_VIEW__:{json.dumps(candidate)}"
    state = {
        "answer": f"Showing the requested residues.\n{marker}",
        "tools": [{"name": "code_interpreter", "result": {"stdout": marker}}],
    }

    # A spec that omits `mode` is a replacement, the pre-layering behavior.
    assert view_specs_for_state(state) == [{**candidate, "mode": "replace"}]
    assert response_without_view_payloads(state["answer"]) == "Showing the requested residues."


def test_invalid_view_spec_is_ignored() -> None:
    marker = '__MOLSTAR_VIEW__: {"include":"all","chains":[],"javascript":"alert(1)"}'

    assert view_specs_for_state({"answer": marker, "tools": []}) == []
    assert response_without_view_payloads(marker) == marker


def test_agent_structure_view_uses_full_structure_without_a_view_request() -> None:
    view = agent_structure_view(MMCIF, {"answer": "Analysis only.", "tools": []})

    assert view["structure"] == MMCIF.encode()
    assert view["caption"] == "Full uploaded structure · 5 atom records"
    assert view["color_theme"] == "chain-id"
    assert view["agent_directed"] is False
    assert view["warning"] is None


def test_agent_structure_view_resolves_the_agent_filter() -> None:
    marker = (
        '__MOLSTAR_VIEW__:{"include":"ligands","chains":[],'
        '"residue_names":["ATP"],"residue_ranges":[],"exclude_water":true,'
        '"color_theme":"element-symbol"}'
    )

    view = agent_structure_view(MMCIF, {"answer": marker, "tools": []})

    assert atom_record_count(view["structure"]) == 1
    assert "ligands · residues ATP · 1 atom records" == view["caption"]
    assert view["color_theme"] == "element-symbol"
    assert view["agent_directed"] is True
    assert view["warning"] is None


def test_agent_structure_view_falls_back_when_the_filter_matches_nothing() -> None:
    marker = (
        '__MOLSTAR_VIEW__:{"include":"protein","chains":["missing"],'
        '"residue_names":[],"residue_ranges":[],"exclude_water":true,'
        '"color_theme":"chain-id"}'
    )

    view = agent_structure_view(MMCIF, {"answer": marker, "tools": []})

    assert view["structure"] == MMCIF.encode()
    assert view["agent_directed"] is False
    assert "Showing the full uploaded structure instead" in view["warning"]


def test_filter_mmcif_selects_protein_by_author_chain_and_range() -> None:
    spec = {
        "include": "protein",
        "chains": ["X"],
        "residue_names": [],
        "residue_ranges": [{"chain": "X", "start": 110, "end": 110}],
        "exclude_water": True,
        "color_theme": "chain-id",
    }

    filtered, count = filter_mmcif(MMCIF.encode(), spec)
    rendered = filtered.decode()

    assert count == 1
    assert "ATOM 1 C CA ALA A 10 ALA X 110" in rendered
    assert "ATOM 2 N N GLY" not in rendered
    assert "HETATM 4" not in rendered
    assert atom_record_count(filtered) == 1
    assert view_description(spec) == "protein · chain X · range X:110–110"


def test_filter_mmcif_uses_label_sequence_for_label_chain_range() -> None:
    spec = {
        "include": "protein",
        "chains": ["A"],
        "residue_names": [],
        "residue_ranges": [{"chain": "A", "start": 10, "end": 10}],
        "exclude_water": True,
        "color_theme": "chain-id",
    }

    filtered, count = filter_mmcif(MMCIF, spec)

    assert count == 1
    assert "ATOM 1 C CA ALA A 10 ALA X 110" in filtered.decode()


def test_filter_mmcif_checks_both_numberings_when_chain_ids_are_identical() -> None:
    same_chain_ids = MMCIF.replace("ALA X 110", "ALA A 110").replace("GLY X 111", "GLY A 111")
    label_spec = {
        "include": "protein",
        "chains": ["A"],
        "residue_names": [],
        "residue_ranges": [{"chain": "A", "start": 10, "end": 10}],
        "exclude_water": True,
        "color_theme": "chain-id",
    }
    author_spec = {
        **label_spec,
        "residue_ranges": [{"chain": "A", "start": 110, "end": 110}],
    }

    label_filtered, label_count = filter_mmcif(same_chain_ids, label_spec)
    author_filtered, author_count = filter_mmcif(same_chain_ids, author_spec)

    assert label_count == author_count == 1
    assert "ATOM 1 C CA ALA A 10 ALA A 110" in label_filtered.decode()
    assert "ATOM 1 C CA ALA A 10 ALA A 110" in author_filtered.decode()


def test_filter_mmcif_chainless_range_accepts_label_or_author_numbering() -> None:
    spec = {
        "include": "protein",
        "chains": [],
        "residue_names": [],
        "residue_ranges": [{"start": 10, "end": 10}],
        "exclude_water": True,
        "color_theme": "chain-id",
    }

    filtered, count = filter_mmcif(MMCIF, spec)

    assert count == 1
    assert "ATOM 1 C CA ALA A 10 ALA X 110" in filtered.decode()


def test_filter_mmcif_can_isolate_ligand_and_excludes_water() -> None:
    spec = {
        "include": "ligands",
        "chains": [],
        "residue_names": ["ATP"],
        "residue_ranges": [],
        "exclude_water": True,
        "color_theme": "element-symbol",
    }

    filtered, count = filter_mmcif(MMCIF, spec)

    assert count == 1
    assert "HETATM 4 C C1 ATP" in filtered.decode()
    assert "HETATM 5 O O HOH" not in filtered.decode()


def test_filter_mmcif_rejects_empty_selection() -> None:
    spec = {
        "include": "protein",
        "chains": ["missing"],
        "residue_names": [],
        "residue_ranges": [],
        "exclude_water": True,
        "color_theme": "chain-id",
    }

    try:
        filter_mmcif(MMCIF, spec)
    except ValueError as exc:
        assert "No atoms matched" in str(exc)
    else:
        raise AssertionError("an empty structure selection must not be rendered")


def test_comparison_view_slots_return_the_managed_view() -> None:
    managed = (
        '__MOLSTAR_VIEW__:{"include":"protein","chains":["A"],'
        '"residue_names":[],"residue_ranges":[],"exclude_water":true,'
        '"color_theme":"chain-id"}'
    )

    slots = prompt_controlled_view_slots(
        {
            "Managed agent": {"answer": managed, "tools": []},
            "Custom agent": {"answer": "No view returned.", "tools": []},
        }
    )

    assert [name for name, _ in slots] == ["Managed agent"]
    assert slots[0][1]["include"] == "protein"


def test_comparison_view_slots_ignore_the_custom_view_when_both_exist() -> None:
    managed = (
        '__MOLSTAR_VIEW__:{"include":"protein","chains":["A"],'
        '"residue_names":[],"residue_ranges":[],"exclude_water":true,'
        '"color_theme":"chain-id"}'
    )
    custom = (
        '__MOLSTAR_VIEW__:{"include":"ligands","chains":[],'
        '"residue_names":["ATP"],"residue_ranges":[],"exclude_water":true,'
        '"color_theme":"element-symbol"}'
    )

    slots = prompt_controlled_view_slots(
        {
            "Custom agent": {"answer": custom, "tools": []},
            "Managed agent": {"answer": managed, "tools": []},
        }
    )

    assert [name for name, _ in slots] == ["Managed agent"]
    assert slots[0][1]["include"] == "protein"


def test_comparison_view_slots_do_not_render_a_custom_only_result() -> None:
    custom = (
        '__MOLSTAR_VIEW__:{"include":"ligands","chains":[],'
        '"residue_names":["ATP"],"residue_ranges":[],"exclude_water":true,'
        '"color_theme":"element-symbol"}'
    )

    assert (
        prompt_controlled_view_slots(
            {
                "Managed agent": {"answer": "No view returned.", "tools": []},
                "Custom agent": {"answer": custom, "tools": []},
            }
        )
        == []
    )


def _spec(**overrides) -> dict:
    spec = {
        "include": "all",
        "chains": [],
        "residue_names": [],
        "residue_ranges": [],
        "exclude_water": True,
        "color_theme": "chain-id",
        "mode": "replace",
    }
    spec.update(overrides)
    return spec


def _marker_state(*candidates: dict) -> dict:
    markers = "\n".join(f"__MOLSTAR_VIEW__:{json.dumps(item)}" for item in candidates)
    return {"answer": f"Here it is.\n{markers}", "tools": []}


def test_layers_union_atoms_that_no_single_spec_could_match() -> None:
    residues = _spec(include="protein", residue_ranges=[{"start": 110, "end": 110}])
    iron = _spec(include="ligands", residue_names=["ATP"])

    # Neither spec alone reaches the other's atoms; ANDing them matches nothing.
    _, residue_atoms = filter_mmcif(MMCIF, residues)
    _, ligand_atoms = filter_mmcif(MMCIF, iron)
    _, union_atoms = filter_mmcif(MMCIF, [residues, iron])

    assert (residue_atoms, ligand_atoms) == (1, 1)
    assert union_atoms == 2


def test_add_mode_keeps_the_previous_layer_and_replace_starts_over() -> None:
    residues = _spec(include="protein", residue_ranges=[{"start": 110, "end": 110}])
    added = _spec(include="ligands", residue_names=["ATP"], mode="add")
    fresh = _spec(include="polymer")

    stacked = compose_view_layers([residues], _marker_state(added))
    assert stacked == [residues, added]

    assert compose_view_layers(stacked, _marker_state(fresh)) == [fresh]


def test_an_add_with_nothing_displayed_yet_becomes_the_first_layer() -> None:
    added = _spec(include="ligands", mode="add")

    assert compose_view_layers([], _marker_state(added)) == [added]
    assert compose_view_layers([added], _marker_state(added)) == [added]


def test_a_turn_without_a_view_request_leaves_the_current_view_standing() -> None:
    residues = _spec(include="protein")

    assert compose_view_layers([residues], {"answer": "Just prose.", "tools": []}) == [residues]


def test_view_layers_are_bounded() -> None:
    layers = [_spec(chains=[f"C{index}"]) for index in range(12)]
    added = _spec(include="ligands", mode="add")

    assert len(compose_view_layers(layers, _marker_state(added))) == 8


def test_an_unknown_mode_is_rejected() -> None:
    candidate = {
        "include": "all",
        "chains": [],
        "residue_names": [],
        "residue_ranges": [],
        "exclude_water": True,
        "color_theme": "chain-id",
        "mode": "merge",
    }

    assert view_specs_for_state(_marker_state(candidate)) == []


def test_the_dashboard_renders_a_stack_and_the_newest_layer_owns_the_theme() -> None:
    residues = _spec(include="protein", residue_ranges=[{"start": 110, "end": 110}])
    ligand = _spec(include="ligands", residue_names=["ATP"], color_theme="element-symbol")

    view = agent_structure_view(MMCIF, {"answer": "", "tools": []}, layers=[residues, ligand])

    assert view["agent_directed"] is True
    assert view["color_theme"] == "element-symbol"
    assert view["caption"].startswith("protein · range 110–110 + ligands · residues ATP")
    assert atom_record_count(view["structure"]) == 2


def test_a_stack_matching_no_atoms_falls_back_to_the_full_structure() -> None:
    impossible = _spec(include="ligands", residue_names=["ZZZ"])

    view = agent_structure_view(MMCIF, {"answer": "", "tools": []}, layers=[impossible])

    assert view["agent_directed"] is False
    assert "Showing the full uploaded structure instead." in view["warning"]
