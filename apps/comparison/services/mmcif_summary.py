"""Deterministic, bounded summaries of uploaded PDBx/mmCIF text.

The comparison agents still receive the original file through their isolated
code interpreter when a question needs coordinates.  This module extracts the
small set of structural metadata that should not require an LLM to improvise a
parser or repeatedly copy a presigned URL.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator
from math import atan2, degrees, isfinite, sqrt
from typing import Any

_SECONDARY_PREFIXES = ("struct_conf", "struct_sheet", "pdbx_struct_sheet")
_MAX_CATEGORY_RECORDS = 100
_MAX_GEOMETRY_RECORDS = 100
_WATER_NAMES = {"DOD", "HOH", "WAT"}
_SAFE_METADATA_TOKEN = re.compile(r"[A-Za-z0-9_.:+-]{1,40}\Z")
_SECONDARY_FIELDS = {
    "id",
    "type",
    "conf_type_id",
    "pdbx_PDB_helix_id",
    "number_strands",
    "sheet_id",
    "range_id_1",
    "range_id_2",
    "sense",
    "offset",
    "beg_label_comp_id",
    "beg_label_asym_id",
    "beg_label_seq_id",
    "beg_auth_comp_id",
    "beg_auth_asym_id",
    "beg_auth_seq_id",
    "pdbx_beg_PDB_ins_code",
    "end_label_comp_id",
    "end_label_asym_id",
    "end_label_seq_id",
    "end_auth_comp_id",
    "end_auth_asym_id",
    "end_auth_seq_id",
    "pdbx_end_PDB_ins_code",
    "range_1_label_atom_id",
    "range_1_label_comp_id",
    "range_1_label_asym_id",
    "range_1_label_seq_id",
    "range_1_auth_atom_id",
    "range_1_auth_comp_id",
    "range_1_auth_asym_id",
    "range_1_auth_seq_id",
    "range_2_label_atom_id",
    "range_2_label_comp_id",
    "range_2_label_asym_id",
    "range_2_label_seq_id",
    "range_2_auth_atom_id",
    "range_2_auth_comp_id",
    "range_2_auth_asym_id",
    "range_2_auth_seq_id",
}


def _line_tokens(line: str) -> Iterator[str]:
    """Tokenize one non-multiline CIF line without treating inner quotes specially."""
    cursor = 0
    while cursor < len(line):
        while cursor < len(line) and line[cursor].isspace():
            cursor += 1
        if cursor >= len(line) or line[cursor] == "#":
            return
        quote = line[cursor] if line[cursor] in {"'", '"'} else None
        if quote:
            cursor += 1
            start = cursor
            while cursor < len(line):
                if line[cursor] == quote and (
                    cursor + 1 == len(line) or line[cursor + 1].isspace()
                ):
                    yield line[start:cursor]
                    cursor += 1
                    break
                cursor += 1
            else:
                raise ValueError("The mmCIF contains an unterminated quoted value.")
            continue
        start = cursor
        while cursor < len(line) and not line[cursor].isspace():
            cursor += 1
        yield line[start:cursor]


def _cif_tokens(text: str) -> list[str]:
    """Return CIF tokens, including semicolon-delimited multiline values."""
    lines = text.splitlines()
    tokens: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.startswith(";"):
            tokens.extend(_line_tokens(line))
            index += 1
            continue
        value_lines = [line[1:]]
        index += 1
        while index < len(lines) and not lines[index].startswith(";"):
            value_lines.append(lines[index])
            index += 1
        if index >= len(lines):
            raise ValueError("The mmCIF contains an unterminated multiline value.")
        tokens.append("\n".join(value_lines))
        index += 1
    return tokens


def _split_header(header: str) -> tuple[str, str]:
    normalized = header.lstrip("_")
    if "." not in normalized:
        return normalized.casefold(), "value"
    category, field = normalized.split(".", 1)
    return category.casefold(), field


def _is_control_token(token: str) -> bool:
    lowered = token.casefold()
    return (
        token.startswith("_")
        or lowered in {"loop_", "stop_", "global_"}
        or lowered.startswith(("data_", "save_"))
    )


def _safe_identifier(value: str) -> str | None:
    if value in {".", "?"}:
        return None
    return value if _SAFE_METADATA_TOKEN.fullmatch(value) else None


def _bounded_row(row: dict[str, str]) -> dict[str, str]:
    return {
        field: safe
        for field, value in row.items()
        if field in _SECONDARY_FIELDS
        if (safe := _safe_identifier(value)) is not None
    }


def _number(value: str | None) -> float | None:
    try:
        parsed = float(value or "")
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) else None


def _sequence_number(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _subtract(left: tuple[float, ...], right: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(a - b for a, b in zip(left, right, strict=True))


def _dot(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _cross(left: tuple[float, ...], right: tuple[float, ...]) -> tuple[float, ...]:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _scale(vector: tuple[float, ...], factor: float) -> tuple[float, ...]:
    return tuple(value * factor for value in vector)


def _dihedral(*points: tuple[float, ...]) -> float | None:
    """Return the signed four-point dihedral in degrees."""
    p0, p1, p2, p3 = points
    b0 = _subtract(p0, p1)
    b1 = _subtract(p2, p1)
    b2 = _subtract(p3, p2)
    norm = sqrt(_dot(b1, b1))
    if norm == 0:
        return None
    b1 = _scale(b1, 1.0 / norm)
    v = _subtract(b0, _scale(b1, _dot(b0, b1)))
    w = _subtract(b2, _scale(b1, _dot(b2, b1)))
    if _dot(v, v) == 0 or _dot(w, w) == 0:
        return None
    return degrees(atan2(_dot(_cross(b1, v), w), _dot(v, w)))


def _geometry_summary(
    residues: dict[tuple[str, str, str, str], dict[str, Any]],
) -> dict[str, Any]:
    """Infer alpha-helical runs from backbone phi/psi angles.

    This is deliberately described as a geometry heuristic, not DSSP. It lets
    the UI answer boundary/disagreement questions without transmitting raw
    coordinates or a temporary credential through an LLM tool call.
    """
    if not residues:
        return {
            "method": "backbone-phi-psi-v1",
            "status": "unavailable",
            "reason": "No usable label-model-chain-residue backbone coordinates were found.",
            "inferred_alpha_helices": [],
        }

    models = sorted({key[0] for key in residues}, key=lambda value: (_sequence_number(value) is None, _sequence_number(value) or 0, value))
    selected_model = models[0]
    chains: dict[str, list[tuple[tuple[str, str, str, str], dict[str, Any]]]] = {}
    for key, residue in residues.items():
        if key[0] == selected_model:
            chains.setdefault(key[1], []).append((key, residue))

    inferred: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    alternates: list[dict[str, Any]] = []
    evaluated = 0
    for chain, chain_residues in sorted(chains.items()):
        chain_residues.sort(
            key=lambda item: (
                _sequence_number(item[0][2]) is None,
                _sequence_number(item[0][2]) or 0,
                item[0][2],
            )
        )
        chosen: list[dict[str, Any]] = []
        for key, residue in chain_residues:
            atoms = {}
            alternate_ids: set[str] = set()
            for atom_name, variants in residue["atoms"].items():
                best = max(
                    variants,
                    key=lambda variant: (
                        variant["occupancy"],
                        variant["alt_id"] in {"", ".", "?", "A"},
                    ),
                )
                atoms[atom_name] = best["xyz"]
                alternate_ids.update(
                    variant["alt_id"]
                    for variant in variants
                    if variant["alt_id"] not in {"", ".", "?"}
                )
            item = {
                "seq_id": key[2],
                "component": key[3],
                "atoms": atoms,
                "has_alternate": len(alternate_ids) > 1,
            }
            chosen.append(item)
            if not {"N", "CA", "C"}.issubset(atoms) and len(missing) < _MAX_GEOMETRY_RECORDS:
                missing.append({"chain": chain, "seq_id": key[2], "component": key[3]})
            if item["has_alternate"] and len(alternates) < _MAX_GEOMETRY_RECORDS:
                alternates.append(
                    {
                        "chain": chain,
                        "seq_id": key[2],
                        "component": key[3],
                        "alternate_ids": sorted(alternate_ids),
                    }
                )

        helical = [False] * len(chosen)
        angles: list[dict[str, float] | None] = [None] * len(chosen)
        for index in range(1, len(chosen) - 1):
            previous, current, following = chosen[index - 1 : index + 2]
            previous_seq = _sequence_number(previous["seq_id"])
            current_seq = _sequence_number(current["seq_id"])
            following_seq = _sequence_number(following["seq_id"])
            if (
                previous_seq is None
                or current_seq is None
                or following_seq is None
                or current_seq - previous_seq != 1
                or following_seq - current_seq != 1
            ):
                continue
            required = (
                previous["atoms"].get("C"),
                current["atoms"].get("N"),
                current["atoms"].get("CA"),
                current["atoms"].get("C"),
                following["atoms"].get("N"),
            )
            if any(point is None for point in required):
                continue
            phi = _dihedral(*required[:4])
            psi = _dihedral(*required[1:])
            if phi is None or psi is None:
                continue
            evaluated += 1
            angles[index] = {"phi": round(phi, 1), "psi": round(psi, 1)}
            helical[index] = -100.0 <= phi <= -30.0 and -80.0 <= psi <= -5.0

        run_start: int | None = None

        def boundary_confidence(residue_index: int, flank_index: int) -> str:
            if chosen[residue_index]["has_alternate"]:
                return "low"
            if flank_index < 0 or flank_index >= len(angles) or angles[flank_index] is None:
                return "low"
            angle = angles[residue_index]
            if angle is None:
                return "low"
            phi_margin = min(angle["phi"] + 100.0, -30.0 - angle["phi"])
            psi_margin = min(angle["psi"] + 80.0, -5.0 - angle["psi"])
            return "high" if min(phi_margin, psi_margin) >= 10.0 else "moderate"

        for index in range(len(chosen) + 1):
            active = index < len(chosen) and helical[index]
            if active and run_start is None:
                run_start = index
            if not active and run_start is not None:
                run_end = index - 1
                if run_end - run_start + 1 >= 3 and len(inferred) < _MAX_GEOMETRY_RECORDS:
                    start = chosen[run_start]
                    end = chosen[run_end]
                    boundary_flags = {
                        "start": boundary_confidence(run_start, run_start - 1),
                        "end": boundary_confidence(run_end, run_end + 1),
                    }
                    inferred.append(
                        {
                            "chain": chain,
                            "beg_label_seq_id": start["seq_id"],
                            "end_label_seq_id": end["seq_id"],
                            "residue_count": run_end - run_start + 1,
                            "boundary_confidence": boundary_flags,
                        }
                    )
                run_start = None

    return {
        "method": "backbone-phi-psi-v1",
        "status": "available" if evaluated else "unavailable",
        "selected_model": selected_model,
        "criterion_degrees": {"phi": [-100, -30], "psi": [-80, -5], "minimum_run": 3},
        "evaluated_residue_count": evaluated,
        "inferred_alpha_helices": inferred,
        "missing_backbone_residue_count": len(missing),
        "missing_backbone_residues": missing,
        "alternate_conformer_residue_count": len(alternates),
        "alternate_conformer_residues": alternates,
        "limitations": "Phi/psi geometry heuristic; it is not DSSP and does not assign hydrogen-bond-based helix classes.",
    }


def summarize_mmcif(data: bytes | str) -> dict[str, Any]:
    """Parse bounded structure metadata and atom-site counts from one mmCIF."""
    text = data.decode("utf-8-sig") if isinstance(data, bytes) else data.lstrip("\ufeff")
    tokens = _cif_tokens(text)
    summary: dict[str, Any] = {
        "schema": "mmcif-summary-v1",
        "data_block": None,
        "secondary_structure": {},
        "atom_site": {
            "row_count": 0,
            "model_ids": [],
            "label_chains": [],
            "author_chains": [],
            "label_residue_count": 0,
            "author_residue_count": 0,
            "non_water_components": [],
        },
        "warnings": [],
    }
    secondary: dict[str, dict[str, Any]] = {}
    singleton_rows: dict[str, dict[str, str]] = {}
    models: set[str] = set()
    label_chains: set[str] = set()
    author_chains: set[str] = set()
    label_residues: set[tuple[str, str, str, str]] = set()
    author_residues: set[tuple[str, str, str, str]] = set()
    hetero_components: Counter[tuple[str, str]] = Counter()
    coordinate_residues: dict[tuple[str, str, str, str], dict[str, Any]] = {}

    def consume_row(category: str, row: dict[str, str]) -> None:
        if category.startswith(_SECONDARY_PREFIXES):
            entry = secondary.setdefault(category, {"count": 0, "records": []})
            entry["count"] += 1
            if len(entry["records"]) < _MAX_CATEGORY_RECORDS:
                entry["records"].append(_bounded_row(row))
            elif entry["count"] == _MAX_CATEGORY_RECORDS + 1:
                summary["warnings"].append(
                    f"{category} records were truncated after {_MAX_CATEGORY_RECORDS} rows."
                )
        if category != "atom_site":
            return
        atom_summary = summary["atom_site"]
        atom_summary["row_count"] += 1
        model = _safe_identifier(row.get("pdbx_PDB_model_num", ""))
        label_chain = _safe_identifier(row.get("label_asym_id", ""))
        author_chain = _safe_identifier(row.get("auth_asym_id", ""))
        label_seq = _safe_identifier(row.get("label_seq_id", ""))
        author_seq = _safe_identifier(row.get("auth_seq_id", ""))
        label_comp = _safe_identifier(row.get("label_comp_id", ""))
        author_comp = _safe_identifier(row.get("auth_comp_id", ""))
        insertion = _safe_identifier(row.get("pdbx_PDB_ins_code", "")) or ""
        if model:
            models.add(model)
        if label_chain:
            label_chains.add(label_chain)
        if author_chain:
            author_chains.add(author_chain)
        if label_chain and label_seq and label_comp:
            label_residues.add((model or "1", label_chain, label_seq, label_comp))
        if author_chain and author_seq and author_comp:
            author_residues.add((model or "1", author_chain, author_seq + insertion, author_comp))
        group = str(row.get("group_PDB", "")).upper()
        component = str(author_comp or label_comp or "").upper()
        chain = str(author_chain or label_chain or "")
        if group == "HETATM" and component and component not in _WATER_NAMES:
            hetero_components[(component, chain)] += 1
        atom_name = _safe_identifier(row.get("label_atom_id", ""))
        x = _number(row.get("Cartn_x"))
        y = _number(row.get("Cartn_y"))
        z = _number(row.get("Cartn_z"))
        if label_chain and label_seq and label_comp and atom_name and None not in {x, y, z}:
            residue = coordinate_residues.setdefault(
                (model or "1", label_chain, label_seq, label_comp), {"atoms": {}}
            )
            residue["atoms"].setdefault(atom_name, []).append(
                {
                    "xyz": (x, y, z),
                    "alt_id": _safe_identifier(row.get("label_alt_id", "")) or "",
                    "occupancy": _number(row.get("occupancy")) or 0.0,
                }
            )

    index = 0
    while index < len(tokens):
        token = tokens[index]
        lowered = token.casefold()
        if lowered.startswith("data_") and summary["data_block"] is None:
            summary["data_block"] = _safe_identifier(token[5:])
            index += 1
            continue
        if lowered == "loop_":
            index += 1
            headers: list[str] = []
            while index < len(tokens) and tokens[index].startswith("_"):
                headers.append(tokens[index])
                index += 1
            if not headers:
                summary["warnings"].append("An empty loop_ declaration was ignored.")
                continue
            categories_and_fields = [_split_header(header) for header in headers]
            categories = {category for category, _ in categories_and_fields}
            mixed_categories = len(categories) != 1
            if mixed_categories:
                summary["warnings"].append("A mixed-category loop was ignored.")
            category = next(iter(categories))
            fields = [field for _, field in categories_and_fields]
            while index < len(tokens) and not _is_control_token(tokens[index]):
                if index + len(fields) > len(tokens):
                    summary["warnings"].append(f"{category} ended with an incomplete row.")
                    index = len(tokens)
                    break
                values = tokens[index : index + len(fields)]
                if any(_is_control_token(value) for value in values):
                    summary["warnings"].append(f"{category} ended with an incomplete row.")
                    break
                if not mixed_categories:
                    consume_row(category, dict(zip(fields, values, strict=True)))
                index += len(fields)
            continue
        if token.startswith("_"):
            category, field = _split_header(token)
            if index + 1 >= len(tokens) or _is_control_token(tokens[index + 1]):
                summary["warnings"].append(f"{token} has no value.")
                index += 1
                continue
            singleton_rows.setdefault(category, {})[field] = tokens[index + 1]
            index += 2
            continue
        index += 1

    for category, row in singleton_rows.items():
        consume_row(category, row)

    atom_summary = summary["atom_site"]
    atom_summary.update(
        {
            "model_ids": sorted(models)[:32],
            "label_chains": sorted(label_chains)[:64],
            "author_chains": sorted(author_chains)[:64],
            "label_residue_count": len(label_residues),
            "author_residue_count": len(author_residues),
            "non_water_components": [
                {"component": component, "chain": chain, "atom_count": count}
                for (component, chain), count in sorted(hetero_components.items())[:64]
            ],
        }
    )
    summary["secondary_structure"] = dict(sorted(secondary.items()))
    summary["geometry_inference"] = _geometry_summary(coordinate_residues)
    return summary
