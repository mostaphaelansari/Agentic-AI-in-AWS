"""Safe structure-notebook command parsing and view-stack behavior."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "comparison"))
sys.path.insert(0, str(ROOT / "opensource" / "ui"))

from structure_notebook import (  # noqa: E402
    NotebookCellError,
    apply_notebook_command,
    parse_notebook_cell,
)


def test_view_cell_is_normalized_as_a_replacement() -> None:
    command = parse_notebook_cell(
        'view(include="protein", chains=["A"], color_theme="secondary-structure")'
    )

    assert command == {
        "action": "view",
        "spec": {
            "include": "protein",
            "chains": ["A"],
            "residue_names": [],
            "residue_ranges": [],
            "exclude_water": True,
            "color_theme": "secondary-structure",
            "mode": "replace",
        },
    }


def test_add_view_cell_keeps_the_existing_layer() -> None:
    first = parse_notebook_cell('view(include="protein", chains=["A"])')
    second = parse_notebook_cell('add_view(include="ligands", residue_names=["HEM"])')

    layers = apply_notebook_command([], first)
    layers = apply_notebook_command(layers, second)

    assert [layer["include"] for layer in layers] == ["protein", "ligands"]
    assert layers[-1]["mode"] == "add"


def test_reset_cell_clears_the_view_stack() -> None:
    command = parse_notebook_cell("reset_view()")

    assert apply_notebook_command([{"mode": "replace"}], command) == []


@pytest.mark.parametrize(
    "source",
    [
        "import os",
        'open("/etc/passwd").read()',
        "view(chains=get_chains())",
        'view(**{"chains": ["A"]})',
        'view({"chains": ["A"]})',
        'view(chains=["A"], mode="add")',
        "unknown()",
    ],
)
def test_executable_or_unsupported_python_is_rejected(source: str) -> None:
    with pytest.raises(NotebookCellError):
        parse_notebook_cell(source)


def test_invalid_view_values_are_rejected() -> None:
    with pytest.raises(NotebookCellError, match="view request is invalid"):
        parse_notebook_cell('view(include="everything", color_theme="hot-pink")')


def test_measure_cell_is_parsed_as_a_measurement() -> None:
    command = parse_notebook_cell('measure(metric="binding_site", ligand="HEM", cutoff=5)')

    assert command == {
        "action": "measure",
        "spec": {
            "metric": "binding_site",
            "chains": [],
            "ligand": "HEM",
            "cutoff": 5.0,
            "residues": [],
        },
    }


def test_an_invalid_measure_cell_is_refused() -> None:
    with pytest.raises(NotebookCellError, match="measurement request is invalid"):
        parse_notebook_cell('measure(metric="sasa")')


def test_a_measure_cell_does_not_change_the_view_stack() -> None:
    command = parse_notebook_cell('measure(metric="summary")')

    with pytest.raises(NotebookCellError, match="does not change the view stack"):
        apply_notebook_command([], command)
