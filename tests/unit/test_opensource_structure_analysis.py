"""Quantitative mmCIF measurements and their declarative request language."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "comparison"))
sys.path.insert(0, str(ROOT / "opensource" / "ui"))

import structure_analysis  # noqa: E402
from molecule_viewer import filter_mmcif, normalize_view_spec  # noqa: E402
from structure_analysis import (  # noqa: E402
    STRUCTURE_METRIC_MARKER,
    StructureAnalysisError,
    _looks_like_predicted_confidence,
    measure,
    metric_description,
    metric_specs_from_result,
    normalize_metric_spec,
    parse_atoms,
    response_without_metric_payloads,
)

_COLUMNS = (
    "group_PDB",
    "id",
    "type_symbol",
    "label_atom_id",
    "label_alt_id",
    "label_comp_id",
    "label_asym_id",
    "label_seq_id",
    "pdbx_PDB_ins_code",
    "Cartn_x",
    "Cartn_y",
    "Cartn_z",
    "occupancy",
    "B_iso_or_equiv",
    "auth_seq_id",
    "auth_comp_id",
    "auth_asym_id",
    "auth_atom_id",
    "pdbx_PDB_model_num",
)

# (group, element, atom, comp, chain, seq, x, y, z)
_ATOMS = (
    ("ATOM", "C", "CA", "HIS", "A", 58, 0.0, 0.0, 0.0),
    ("ATOM", "N", "NE2", "HIS", "A", 58, 2.0, 0.0, 0.0),
    ("ATOM", "C", "CA", "ASP", "A", 60, 10.0, 0.0, 0.0),
    ("ATOM", "O", "OD1", "ASP", "A", 60, 11.0, 0.0, 0.0),
    ("ATOM", "N", "NH1", "ARG", "A", 62, 13.8, 0.0, 0.0),
    ("ATOM", "S", "SG", "CYS", "A", 10, 0.0, 10.0, 0.0),
    ("ATOM", "S", "SG", "CYS", "A", 20, 0.0, 12.0, 0.0),
    ("ATOM", "C", "CA", "GLY", "B", 5, 4.0, 3.0, 0.0),
    ("HETATM", "FE", "FE", "HEM", "A", 200, 4.0, 0.0, 0.0),
    ("HETATM", "O", "O", "HOH", "A", 300, 50.0, 50.0, 50.0),
)


def _structure() -> bytes:
    lines = ["data_test", "#", "loop_"]
    lines.extend(f"_atom_site.{column}" for column in _COLUMNS)
    for index, (group, element, atom, comp, chain, seq, x, y, z) in enumerate(_ATOMS, start=1):
        lines.append(
            " ".join(
                str(value)
                for value in (
                    group,
                    index,
                    element,
                    atom,
                    ".",
                    comp,
                    chain,
                    seq,
                    "?",
                    x,
                    y,
                    z,
                    "1.00",
                    "20.00",
                    seq,
                    comp,
                    chain,
                    atom,
                    1,
                )
            )
        )
    lines.append("#")
    return ("\n".join(lines) + "\n").encode("utf-8")


STRUCTURE = _structure()


def _rows(metric: str, **overrides: object) -> dict[str, object]:
    return measure(STRUCTURE, {"metric": metric, **overrides})


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_atoms_are_parsed_with_classification() -> None:
    atoms = parse_atoms(STRUCTURE)

    assert len(atoms) == len(_ATOMS)
    assert sum(1 for atom in atoms if atom.is_ligand) == 1
    assert sum(1 for atom in atoms if atom.is_water) == 1
    assert atoms[0].residue_label == "A:HIS58"


def test_a_file_without_an_atom_site_loop_is_rejected() -> None:
    with pytest.raises(StructureAnalysisError, match="_atom_site"):
        parse_atoms(b"data_empty\n#\n")


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


def test_summary_counts_polymer_ligand_and_water_separately() -> None:
    result = _rows("summary")

    values = {row["property"]: row["value"] for row in result["rows"]}
    assert values["Polymer chains"].startswith("2")
    assert values["Ligand atoms"] == "1"
    assert values["Water molecules"] == "1"


def test_ligand_inventory_excludes_water_and_offers_a_view() -> None:
    result = _rows("ligands")

    assert [row["ligand"] for row in result["rows"]] == ["HEM"]
    assert result["view_layers"][0]["residue_names"] == ["HEM"]


def test_binding_site_ranks_residues_by_closest_approach() -> None:
    result = _rows("binding_site", ligand="HEM", cutoff=4.5)

    assert result["rows"][0]["residue"] == "HIS58"
    assert result["rows"][0]["distance_A"] == 2.0
    assert result["rows"][0]["ligand_atom"] == "FE"
    # Chain B contributes to the same pocket, so a pocket is not chain-scoped.
    assert {row["chain"] for row in result["rows"]} == {"A", "B"}


def test_binding_site_view_layers_render_the_pocket_and_the_ligand() -> None:
    layers = _rows("binding_site", ligand="HEM")["view_layers"]

    assert all(normalize_view_spec(layer) == layer for layer in layers)
    assert layers[1]["include"] == "ligands"
    assert layers[1]["mode"] == "add"
    _, matched = filter_mmcif(STRUCTURE, layers)
    assert matched > 0


def test_an_absent_ligand_names_the_ones_present() -> None:
    with pytest.raises(StructureAnalysisError, match="HEM"):
        _rows("binding_site", ligand="ATP")


def test_distance_reports_closest_approach_and_centroids() -> None:
    result = _rows(
        "distance",
        residues=[{"chain": "A", "seq": 58}, {"chain": "A", "seq": 60}],
    )

    measurements = {row["measurement"]: row["value_A"] for row in result["rows"]}
    assert measurements["Closest approach"] == 8.0
    assert measurements["Cα–Cα"] == 10.0


def test_distance_can_target_one_named_atom() -> None:
    result = _rows(
        "distance",
        residues=[{"chain": "A", "seq": 58, "atom": "NE2"}, {"chain": "A", "seq": 60}],
    )

    assert result["rows"][0]["value_A"] == 8.0
    assert "NE2" in result["headline"] or "NE2" in result["rows"][0]["detail"]


def test_an_unknown_residue_is_reported_not_guessed() -> None:
    with pytest.raises(StructureAnalysisError, match="not found"):
        _rows("distance", residues=[{"chain": "A", "seq": 999}, {"chain": "A", "seq": 60}])


def test_contacts_need_two_chains_and_find_the_interface() -> None:
    result = _rows("contacts", chains=["A", "B"], cutoff=4.0)

    assert result["rows"][0]["residue_b"].startswith("B:GLY5")
    with pytest.raises(StructureAnalysisError, match="exactly two chains"):
        _rows("contacts", chains=["A"])


def test_polar_contacts_separate_disulfides_and_salt_bridges() -> None:
    result = _rows("polar_contacts", cutoff=3.5)

    kinds = {row["kind"] for row in result["rows"]}
    assert "disulfide" in kinds
    assert "salt bridge" in kinds
    # Without hydrogens these are candidates; the headline must not overclaim.
    assert "not confirmed" in result["headline"]


def test_geometry_reports_a_finite_radius_of_gyration() -> None:
    values = {row["property"]: row["value"] for row in _rows("geometry")["rows"]}

    assert values["Radius of gyration"].endswith("Å")
    assert float(values["Radius of gyration"].split()[0]) > 0


def test_quality_reports_chain_breaks() -> None:
    values = {row["property"]: row["value"] for row in _rows("quality")["rows"]}

    assert "A:11–19" in values["Chain breaks (unmodeled residues)"]


# --------------------------------------------------------------------------
# Request validation
# --------------------------------------------------------------------------


def test_defaults_are_filled_in() -> None:
    assert normalize_metric_spec({"metric": "binding_site"}) == {
        "metric": "binding_site",
        "chains": [],
        "ligand": "",
        "cutoff": 4.5,
        "residues": [],
    }


@pytest.mark.parametrize(
    "candidate",
    [
        {"metric": "sasa"},
        {"metric": "summary", "unexpected": 1},
        {"metric": "binding_site", "cutoff": 40},
        {"metric": "binding_site", "cutoff": "4.5"},
        {"metric": "distance", "residues": [{"chain": "A", "seq": 1}]},
        {"metric": "distance", "residues": [{"chain": "A"}, {"chain": "A", "seq": 2}]},
        {"metric": "summary", "chains": "A"},
        {"metric": "summary", "chains": [1]},
        "not a mapping",
    ],
)
def test_invalid_requests_are_refused(candidate: object) -> None:
    assert normalize_metric_spec(candidate) is None


def test_measure_refuses_an_invalid_request() -> None:
    with pytest.raises(StructureAnalysisError, match="invalid"):
        measure(STRUCTURE, {"metric": "nonsense"})


# --------------------------------------------------------------------------
# Agent protocol
# --------------------------------------------------------------------------


def test_markers_are_extracted_from_nested_agent_output() -> None:
    payload = {
        "content": [
            {"text": f'Here it is {STRUCTURE_METRIC_MARKER}{{"metric":"ligands"}} done.'},
        ]
    }

    assert [spec["metric"] for spec in metric_specs_from_result(payload)] == ["ligands"]


def test_invalid_markers_are_ignored() -> None:
    text = f'{STRUCTURE_METRIC_MARKER}{{"metric":"sasa"}}'

    assert metric_specs_from_result(text) == []
    assert response_without_metric_payloads(text) == text.strip()


def test_valid_markers_are_stripped_from_prose() -> None:
    text = f'The pocket. {STRUCTURE_METRIC_MARKER}{{"metric":"binding_site"}} Rendered.'

    assert response_without_metric_payloads(text) == "The pocket.  Rendered."


def test_metric_description_names_what_was_measured() -> None:
    spec = normalize_metric_spec({"metric": "binding_site", "ligand": "HEM", "cutoff": 5})

    assert metric_description(spec) == "binding site · HEM · 5.0 Å"


def _many_residues() -> list:
    """The fixture repeated so the heuristic has enough multi-atom residues."""
    atoms = []
    for copy in range(6):
        atoms.extend(
            dataclasses.replace(atom, seq=(atom.seq or 0) + 1000 * copy)
            for atom in parse_atoms(STRUCTURE)
        )
    return atoms


def test_per_atom_b_factors_are_not_reported_as_prediction_confidence() -> None:
    atoms = [
        dataclasses.replace(atom, b_factor=20.0 + index)
        for index, atom in enumerate(_many_residues())
    ]

    assert _looks_like_predicted_confidence(atoms) is False


def test_one_confidence_per_residue_in_0_100_is_read_as_pLDDT() -> None:
    atoms = [
        dataclasses.replace(atom, b_factor=float(50 + (atom.seq or 0) % 40))
        for atom in _many_residues()
    ]

    assert _looks_like_predicted_confidence(atoms) is True


def test_b_factors_outside_the_confidence_scale_are_never_pLDDT() -> None:
    atoms = [dataclasses.replace(atom, b_factor=140.0) for atom in _many_residues()]

    assert _looks_like_predicted_confidence(atoms) is False


def test_an_oversized_search_is_reported_not_run_to_exhaustion(monkeypatch) -> None:
    monkeypatch.setattr(structure_analysis, "MAX_PAIRS", 1)

    with pytest.raises(StructureAnalysisError, match="too large to measure"):
        _rows("polar_contacts", cutoff=12)
