"""Safe, Jupyter-style cells for controlling the answer-side Mol* view.

Cells are parsed as Python syntax but never executed. Only a single call to the
small declarative API below is accepted, keeping the UI container from becoming
an arbitrary-code execution service.
"""

from __future__ import annotations

import ast
from typing import Any

from molecule_viewer import normalize_view_spec
from structure_analysis import normalize_metric_spec

MAX_NOTEBOOK_LAYERS = 8
DEFAULT_NOTEBOOK_CELL = """view(
    include="protein",
    chains=["A"],
    residue_ranges=[],
    residue_names=[],
    exclude_water=True,
    color_theme="chain-id",
)"""


class NotebookCellError(ValueError):
    """A notebook cell is not part of the supported data-only view API."""


def _literal_keywords(call: ast.Call) -> dict[str, Any]:
    if call.args:
        raise NotebookCellError('Use named arguments only, for example chains=["A"].')
    values: dict[str, Any] = {}
    for keyword in call.keywords:
        if keyword.arg is None:
            raise NotebookCellError("Expanded **kwargs are not supported.")
        if keyword.arg in values:
            raise NotebookCellError(f"Argument {keyword.arg!r} was provided more than once.")
        try:
            values[keyword.arg] = ast.literal_eval(keyword.value)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError) as exc:
            raise NotebookCellError(
                f"Argument {keyword.arg!r} must be a literal value, not executable code."
            ) from exc
    return values


def parse_notebook_cell(source: str) -> dict[str, Any]:
    """Parse one safe ``view``, ``add_view``, or ``reset_view`` cell."""
    if not source.strip():
        raise NotebookCellError("The cell is empty.")
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        message = exc.msg or "invalid Python syntax"
        raise NotebookCellError(f"Python syntax error: {message}.") from exc
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.Expr):
        raise NotebookCellError("A cell must contain one view API call.")
    expression = tree.body[0].value
    if not isinstance(expression, ast.Call) or not isinstance(expression.func, ast.Name):
        raise NotebookCellError("Call view(...), add_view(...), measure(...), or reset_view().")

    function = expression.func.id
    if function == "reset_view":
        if expression.args or expression.keywords:
            raise NotebookCellError("reset_view() does not accept arguments.")
        return {"action": "reset", "spec": None}
    if function == "measure":
        candidate = _literal_keywords(expression)
        spec = normalize_metric_spec(candidate)
        if spec is None:
            raise NotebookCellError(
                "The measurement request is invalid. Check the metric name, chains, "
                "ligand, cutoff, and residues."
            )
        return {"action": "measure", "spec": spec}
    if function not in {"view", "add_view"}:
        raise NotebookCellError(
            "Only view(...), add_view(...), measure(...), and reset_view() are available."
        )

    candidate = _literal_keywords(expression)
    if "mode" in candidate:
        raise NotebookCellError("Use view(...) to replace or add_view(...) to add a layer.")
    candidate["mode"] = "add" if function == "add_view" else "replace"
    spec = normalize_view_spec(candidate)
    if spec is None:
        raise NotebookCellError(
            "The view request is invalid. Check the include value, chains, residue ranges, "
            "residue names, and color theme."
        )
    return {"action": "view", "spec": spec}


def apply_notebook_command(
    previous: list[dict[str, Any]], command: dict[str, Any]
) -> list[dict[str, Any]]:
    """Apply a parsed cell to the view stack shown by the answer."""
    if command.get("action") == "reset":
        return []
    if command.get("action") == "measure":
        # A measurement reports numbers; the view it implies is applied from the
        # measurement result, which knows which residues actually matched.
        raise NotebookCellError("A measure(...) cell does not change the view stack directly.")
    spec = command.get("spec")
    if not isinstance(spec, dict):
        raise NotebookCellError("The parsed cell does not contain a view request.")
    if spec["mode"] == "replace" or not previous:
        return [spec]
    layers = list(previous)
    if spec not in layers:
        layers.append(spec)
    return layers[-MAX_NOTEBOOK_LAYERS:]
