"""Quantitative measurements over an uploaded mmCIF structure.

The 3D view answers "what does this look like"; this module answers "what are
the numbers". It follows the same trust boundary as `molecule_viewer`: a model
(or a notebook cell) sends a small declarative request, trusted Python
validates it and does the arithmetic over the original mmCIF bytes. Nothing
here executes model-supplied expressions.

Every metric is computed from `_atom_site` coordinates with the standard
library only, so the open-source stack keeps running on a CPU-only machine
with no scientific packages installed.
"""

from __future__ import annotations

import json
import math
import shlex
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

# Reused read-only so residue classification cannot drift between the viewer
# and the measurements taken on the same file.
from molecule_viewer import _PROTEIN_RESIDUES as PROTEIN_RESIDUES
from molecule_viewer import _WATER_NAMES as WATER_NAMES

STRUCTURE_METRIC_MARKER = "__STRUCTURE_METRIC__:"

ALLOWED_METRICS = {
    "summary",
    "ligands",
    "binding_site",
    "distance",
    "contacts",
    "polar_contacts",
    "geometry",
    "quality",
}

# A pocket is reported as residues, and the view language carries at most 32
# residue ranges per layer, so the same answer can always be rendered.
MAX_VIEW_RANGES = 32
MAX_RESULT_ROWS = 250
# Ceiling on atom pairs examined by one measurement. A 12 Å search over a large
# complex would otherwise exhaust memory and take the Streamlit process with it.
MAX_PAIRS = 40_000_000
# Cutoffs outside this band stop describing contact: below it nothing is found,
# above it every residue is a "neighbor" of every other.
MIN_CUTOFF = 1.0
MAX_CUTOFF = 12.0
DEFAULT_POCKET_CUTOFF = 4.5
DEFAULT_CONTACT_CUTOFF = 4.0
DEFAULT_POLAR_CUTOFF = 3.5

# Atoms that can donate or accept a hydrogen bond. Uploaded structures are
# usually X-ray models without hydrogens, so contacts between these atoms are
# reported as polar contacts, not as verified hydrogen bonds.
POLAR_ELEMENTS = {"N", "O"}
_SALT_BRIDGE_ANIONS = {
    ("ASP", "OD1"),
    ("ASP", "OD2"),
    ("GLU", "OE1"),
    ("GLU", "OE2"),
}
_SALT_BRIDGE_CATIONS = {
    ("ARG", "NE"),
    ("ARG", "NH1"),
    ("ARG", "NH2"),
    ("LYS", "NZ"),
    ("HIS", "ND1"),
    ("HIS", "NE2"),
}
_DISULFIDE_CUTOFF = 2.5

# Standard atomic masses for the elements that appear in biomolecular models;
# anything else falls back to carbon so a center of mass stays finite.
_ATOMIC_MASS = {
    "H": 1.008,
    "C": 12.011,
    "N": 14.007,
    "O": 15.999,
    "NA": 22.990,
    "MG": 24.305,
    "P": 30.974,
    "S": 32.06,
    "CL": 35.45,
    "K": 39.098,
    "CA": 40.078,
    "MN": 54.938,
    "FE": 55.845,
    "CO": 58.933,
    "NI": 58.693,
    "CU": 63.546,
    "ZN": 65.38,
    "SE": 78.971,
}
_DEFAULT_MASS = 12.011


class StructureAnalysisError(ValueError):
    """A measurement request is invalid or cannot be answered from the file."""


@dataclass(frozen=True)
class Atom:
    """One `_atom_site` row reduced to the fields measurements need."""

    model: int
    group: str
    name: str
    element: str
    comp: str
    chain: str
    label_chain: str
    seq: int | None
    insertion: str
    x: float
    y: float
    z: float
    occupancy: float | None
    b_factor: float | None
    alt: str
    # Position in the file: a stable identity for ordering a pair of atoms.
    serial: int = 0

    @property
    def is_water(self) -> bool:
        return self.comp in WATER_NAMES

    @property
    def is_polymer(self) -> bool:
        return self.group == "ATOM" or self.comp in PROTEIN_RESIDUES

    @property
    def is_ligand(self) -> bool:
        return self.group == "HETATM" and not self.is_water and self.comp not in PROTEIN_RESIDUES

    @property
    def residue_key(self) -> tuple[int, str, int | None, str, str]:
        return (self.model, self.chain, self.seq, self.insertion, self.comp)

    @property
    def residue_label(self) -> str:
        position = "?" if self.seq is None else f"{self.seq}{self.insertion}"
        return f"{self.chain}:{self.comp}{position}"


# --------------------------------------------------------------------------
# mmCIF atom-site parsing
# --------------------------------------------------------------------------


def _clean(value: str) -> str:
    return "" if value in {"", ".", "?"} else value


def _first(row: dict[str, str], *names: str) -> str:
    for name in names:
        if cleaned := _clean(row.get(name, "")):
            return cleaned
    return ""


def _as_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: str) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def iter_atom_site_rows(data: bytes | str) -> Iterator[dict[str, str]]:
    """Yield `_atom_site` rows of an mmCIF file as name/value mappings."""
    text = data.decode("utf-8-sig", errors="replace") if isinstance(data, bytes) else data
    lines = text.lstrip("﻿").splitlines()

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
        row_lines: list[str] = []
        cursor = header_end
        while cursor < len(lines):
            stripped = lines[cursor].strip()
            if not row_lines and (
                stripped == "#"
                or not stripped
                or stripped.lower() == "loop_"
                or stripped.lower().startswith(("data_", "save_"))
                or stripped.startswith("_")
            ):
                if stripped and stripped != "#":
                    break
                cursor += 1
                if stripped == "#":
                    break
                continue
            if stripped.startswith(";"):
                raise StructureAnalysisError(
                    "Multiline atom-site values are not supported for measurements."
                )
            row_lines.append(lines[cursor])
            try:
                values = shlex.split(" ".join(line.strip() for line in row_lines), posix=True)
            except ValueError as exc:
                raise StructureAnalysisError(
                    "The atom-site table could not be parsed safely."
                ) from exc
            if len(values) < len(names):
                cursor += 1
                continue
            if len(values) > len(names):
                raise StructureAnalysisError("The atom-site table has an unsupported row layout.")
            yield dict(zip(names, values))
            row_lines = []
            cursor += 1
        return
    raise StructureAnalysisError("No `_atom_site` loop was found in the uploaded mmCIF file.")


def parse_atoms(data: bytes | str) -> list[Atom]:
    """Parse an mmCIF file into atoms carrying usable coordinates."""
    atoms: list[Atom] = []
    for serial, row in enumerate(iter_atom_site_rows(data)):
        x = _as_float(_first(row, "Cartn_x"))
        y = _as_float(_first(row, "Cartn_y"))
        z = _as_float(_first(row, "Cartn_z"))
        if x is None or y is None or z is None:
            continue
        chain = _first(row, "auth_asym_id", "label_asym_id")
        atoms.append(
            Atom(
                model=_as_int(_first(row, "pdbx_PDB_model_num")) or 1,
                group=_first(row, "group_PDB").upper() or "ATOM",
                name=_first(row, "auth_atom_id", "label_atom_id").upper(),
                element=_first(row, "type_symbol").upper(),
                comp=_first(row, "auth_comp_id", "label_comp_id").upper(),
                chain=chain,
                label_chain=_first(row, "label_asym_id"),
                seq=_as_int(_first(row, "auth_seq_id", "label_seq_id")),
                insertion=_first(row, "pdbx_PDB_ins_code"),
                x=x,
                y=y,
                z=z,
                occupancy=_as_float(_first(row, "occupancy")),
                b_factor=_as_float(_first(row, "B_iso_or_equiv")),
                alt=_first(row, "label_alt_id"),
                serial=serial,
            )
        )
    if not atoms:
        raise StructureAnalysisError("The uploaded structure contains no usable coordinates.")
    return atoms


def first_model(atoms: Iterable[Atom]) -> list[Atom]:
    """Restrict to the first model so NMR ensembles do not multiply contacts."""
    kept = list(atoms)
    if not kept:
        return kept
    lowest = min(atom.model for atom in kept)
    return [atom for atom in kept if atom.model == lowest]


# --------------------------------------------------------------------------
# Neighbor search
# --------------------------------------------------------------------------


class _Grid:
    """Uniform-cell spatial hash: contact search without a numpy dependency."""

    def __init__(self, atoms: list[Atom], cell: float) -> None:
        self._cell = max(cell, 0.5)
        self._buckets: dict[tuple[int, int, int], list[Atom]] = {}
        for atom in atoms:
            self._buckets.setdefault(self._key(atom.x, atom.y, atom.z), []).append(atom)

    def _key(self, x: float, y: float, z: float) -> tuple[int, int, int]:
        size = self._cell
        return (int(math.floor(x / size)), int(math.floor(y / size)), int(math.floor(z / size)))

    def near(self, x: float, y: float, z: float) -> Iterator[Atom]:
        cx, cy, cz = self._key(x, y, z)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    yield from self._buckets.get((cx + dx, cy + dy, cz + dz), ())


def _distance(a: Atom, b: Atom) -> float:
    return math.dist((a.x, a.y, a.z), (b.x, b.y, b.z))


def atom_pairs_within(
    query: list[Atom],
    target: list[Atom],
    cutoff: float,
) -> Iterator[tuple[Atom, Atom, float]]:
    """Yield (query atom, target atom, distance) for every pair within cutoff.

    This streams rather than returning a list: pair counts grow with the cube of
    the cutoff, so a wide search over a large selection can reach tens of
    millions of pairs. Every caller reduces pairs to per-residue minima as they
    arrive, so nothing needs them all in memory at once. `MAX_PAIRS` still caps
    the search, because a request that large is a mistake worth reporting rather
    than an out-of-memory kill of the whole UI process.
    """
    if not query or not target:
        return
    grid = _Grid(target, cutoff)
    examined = 0
    for atom in query:
        for candidate in grid.near(atom.x, atom.y, atom.z):
            if candidate is atom:
                continue
            examined += 1
            if examined > MAX_PAIRS:
                raise StructureAnalysisError(
                    "This selection is too large to measure. Narrow it to specific "
                    "chains or use a smaller cutoff."
                )
            separation = _distance(atom, candidate)
            if separation <= cutoff:
                yield atom, candidate, separation


# --------------------------------------------------------------------------
# Request validation
# --------------------------------------------------------------------------


def _safe_token(value: Any, *, upper: bool = False, limit: int = 12) -> str | None:
    if value is None:
        return ""
    if not isinstance(value, str) or not (token := value.strip()) or len(token) > limit:
        return None
    return token.upper() if upper else token


def _safe_cutoff(value: Any, default: float) -> float | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or not MIN_CUTOFF <= number <= MAX_CUTOFF:
        return None
    return number


def _safe_residue_ref(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) - {"chain", "seq", "comp", "atom"}:
        return None
    seq = value.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or not -100_000 <= seq <= 100_000:
        return None
    chain = _safe_token(value.get("chain"))
    comp = _safe_token(value.get("comp"), upper=True)
    atom = _safe_token(value.get("atom"), upper=True)
    if chain is None or comp is None or atom is None:
        return None
    return {"chain": chain, "seq": seq, "comp": comp, "atom": atom}


def normalize_metric_spec(candidate: Any) -> dict[str, Any] | None:
    """Validate the declarative measurement language accepted from an agent."""
    if not isinstance(candidate, dict):
        return None
    allowed_keys = {"metric", "chains", "ligand", "cutoff", "residues", "chain"}
    if set(candidate) - allowed_keys:
        return None
    metric = str(candidate.get("metric", "")).strip().lower()
    if metric not in ALLOWED_METRICS:
        return None

    chains_value = candidate.get("chains")
    if chains_value is None:
        chains: list[str] = []
    elif isinstance(chains_value, list) and len(chains_value) <= 8:
        chains = []
        for item in chains_value:
            token = _safe_token(item)
            if not token:
                return None
            chains.append(token)
        chains = list(dict.fromkeys(chains))
    else:
        return None

    if (single_chain := _safe_token(candidate.get("chain"))) is None:
        return None
    if single_chain and single_chain not in chains:
        chains.append(single_chain)

    if (ligand := _safe_token(candidate.get("ligand"), upper=True)) is None:
        return None

    defaults = {
        "binding_site": DEFAULT_POCKET_CUTOFF,
        "contacts": DEFAULT_CONTACT_CUTOFF,
        "polar_contacts": DEFAULT_POLAR_CUTOFF,
    }
    cutoff = _safe_cutoff(candidate.get("cutoff"), defaults.get(metric, DEFAULT_CONTACT_CUTOFF))
    if cutoff is None:
        return None

    raw_residues = candidate.get("residues", [])
    if not isinstance(raw_residues, list) or len(raw_residues) > 2:
        return None
    residues = []
    for item in raw_residues:
        reference = _safe_residue_ref(item)
        if reference is None:
            return None
        residues.append(reference)
    if metric == "distance" and len(residues) != 2:
        return None

    return {
        "metric": metric,
        "chains": chains,
        "ligand": ligand,
        "cutoff": cutoff,
        "residues": residues,
    }


# --------------------------------------------------------------------------
# View specs derived from a measurement
# --------------------------------------------------------------------------


def _residue_ranges(residues: Iterable[tuple[str, int]]) -> list[dict[str, Any]]:
    """Collapse (chain, seq) residues into contiguous ranges for the viewer."""
    by_chain: dict[str, list[int]] = {}
    for chain, seq in residues:
        by_chain.setdefault(chain, []).append(seq)
    ranges: list[dict[str, Any]] = []
    for chain, sequences in sorted(by_chain.items()):
        ordered = sorted(set(sequences))
        start = previous = ordered[0]
        for seq in ordered[1:]:
            if seq == previous + 1:
                previous = seq
                continue
            ranges.append({"chain": chain, "start": start, "end": previous})
            start = previous = seq
        ranges.append({"chain": chain, "start": start, "end": previous})
    return ranges[:MAX_VIEW_RANGES]


def _view_layer(**overrides: Any) -> dict[str, Any]:
    layer = {
        "include": "all",
        "chains": [],
        "residue_names": [],
        "residue_ranges": [],
        "exclude_water": True,
        "color_theme": "chain-id",
        "mode": "replace",
    }
    layer.update(overrides)
    return layer


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


def _selected(atoms: list[Atom], chains: list[str]) -> list[Atom]:
    if not chains:
        return atoms
    wanted = set(chains)
    kept = [atom for atom in atoms if atom.chain in wanted or atom.label_chain in wanted]
    if not kept:
        raise StructureAnalysisError(
            f"No atoms were found in chain {', '.join(chains)}. "
            f"Available chains: {', '.join(sorted({atom.chain for atom in atoms if atom.chain}))}."
        )
    return kept


def _summary(atoms: list[Atom], spec: dict[str, Any]) -> dict[str, Any]:
    scoped = _selected(atoms, spec["chains"])
    models = sorted({atom.model for atom in scoped})
    single = [atom for atom in scoped if atom.model == models[0]]
    polymer = [atom for atom in single if atom.is_polymer and not atom.is_water]
    ligands = [atom for atom in single if atom.is_ligand]
    waters = {atom.residue_key for atom in single if atom.is_water}
    residues = {atom.residue_key for atom in polymer}
    chains = sorted({atom.chain for atom in polymer if atom.chain})

    rows = [
        {"property": "Models", "value": str(len(models))},
        {"property": "Atoms (first model)", "value": f"{len(single):,}"},
        {"property": "Polymer chains", "value": f"{len(chains)} ({', '.join(chains) or 'n/a'})"},
        {"property": "Polymer residues", "value": f"{len(residues):,}"},
        {"property": "Ligand atoms", "value": f"{len(ligands):,}"},
        {"property": "Water molecules", "value": f"{len(waters):,}"},
    ]
    return {
        "headline": (
            f"{len(chains)} chain(s), {len(residues):,} polymer residues, "
            f"{len(ligands):,} ligand atoms, {len(waters):,} waters"
        ),
        "columns": ["property", "value"],
        "rows": rows,
        "view_layers": [],
    }


def _ligands(atoms: list[Atom], spec: dict[str, Any]) -> dict[str, Any]:
    scoped = first_model(_selected(atoms, spec["chains"]))
    groups: dict[tuple[int, str, int | None, str, str], list[Atom]] = {}
    for atom in scoped:
        if atom.is_ligand:
            groups.setdefault(atom.residue_key, []).append(atom)
    if not groups:
        return {
            "headline": "No non-water ligands were found in this structure.",
            "columns": ["ligand", "chain", "residue", "atoms", "elements"],
            "rows": [],
            "view_layers": [],
        }

    rows = []
    for (_, chain, seq, insertion, comp), members in sorted(
        groups.items(), key=lambda item: (-len(item[1]), item[0][1], item[0][2] or 0)
    ):
        elements = sorted({atom.element for atom in members if atom.element})
        rows.append(
            {
                "ligand": comp,
                "chain": chain or "?",
                "residue": "?" if seq is None else f"{seq}{insertion}",
                "atoms": len(members),
                "elements": ", ".join(elements),
            }
        )
    names = sorted({str(row["ligand"]) for row in rows})
    return {
        "headline": f"{len(rows)} ligand copies of {len(names)} component(s): {', '.join(names)}",
        "columns": ["ligand", "chain", "residue", "atoms", "elements"],
        "rows": rows[:MAX_RESULT_ROWS],
        "view_layers": [
            _view_layer(include="ligands", residue_names=names[:32], color_theme="element-symbol")
        ],
    }


def _binding_site(atoms: list[Atom], spec: dict[str, Any]) -> dict[str, Any]:
    scoped = first_model(atoms)
    ligand_atoms = [atom for atom in scoped if atom.is_ligand]
    if spec["ligand"]:
        ligand_atoms = [atom for atom in ligand_atoms if atom.comp == spec["ligand"]]
    if spec["chains"]:
        wanted = set(spec["chains"])
        ligand_atoms = [
            atom for atom in ligand_atoms if atom.chain in wanted or atom.label_chain in wanted
        ]
    if not ligand_atoms:
        available = sorted({atom.comp for atom in scoped if atom.is_ligand})
        target = spec["ligand"] or "any ligand"
        raise StructureAnalysisError(
            f"No atoms were found for {target}. "
            + (
                f"Ligands present: {', '.join(available)}."
                if available
                else "This structure has no non-water ligands."
            )
        )

    ligand_keys = {atom.residue_key for atom in ligand_atoms}
    polymer = [atom for atom in scoped if atom.is_polymer and not atom.is_water]
    cutoff = spec["cutoff"]

    closest: dict[tuple[int, str, int | None, str, str], tuple[float, str, str]] = {}
    for polymer_atom, ligand_atom, separation in atom_pairs_within(polymer, ligand_atoms, cutoff):
        if polymer_atom.residue_key in ligand_keys:
            continue
        key = polymer_atom.residue_key
        if key not in closest or separation < closest[key][0]:
            closest[key] = (separation, polymer_atom.name, ligand_atom.name)

    if not closest:
        raise StructureAnalysisError(
            f"No polymer residue lies within {cutoff:.1f} Å of the selected ligand. "
            "Try a larger cutoff."
        )

    rows = []
    for (_, chain, seq, insertion, comp), (separation, atom_name, partner) in sorted(
        closest.items(), key=lambda item: item[1][0]
    ):
        rows.append(
            {
                "residue": f"{comp}{'' if seq is None else seq}{insertion}",
                "chain": chain or "?",
                "distance_A": round(separation, 2),
                "closest_atom": atom_name,
                "ligand_atom": partner,
            }
        )

    ligand_names = sorted({atom.comp for atom in ligand_atoms})
    pocket = [(chain, seq) for (_, chain, seq, _, _) in closest if chain and seq is not None]
    layers = [
        _view_layer(
            include="protein",
            residue_ranges=_residue_ranges(pocket),
            color_theme="residue-name",
        ),
        _view_layer(include="ligands", residue_names=ligand_names[:32], mode="add"),
    ]
    return {
        "headline": (
            f"{len(rows)} residues within {cutoff:.1f} Å of "
            f"{', '.join(ligand_names)} ({len(ligand_atoms)} ligand atoms)"
        ),
        "columns": ["residue", "chain", "distance_A", "closest_atom", "ligand_atom"],
        "rows": rows[:MAX_RESULT_ROWS],
        "view_layers": layers,
    }


def _residue_atoms(atoms: list[Atom], reference: dict[str, Any]) -> list[Atom]:
    matched = [
        atom
        for atom in atoms
        if atom.seq == reference["seq"]
        and (not reference["chain"] or reference["chain"] in {atom.chain, atom.label_chain})
        and (not reference["comp"] or atom.comp == reference["comp"])
    ]
    if not matched:
        label = f"{reference['chain'] or '?'}:{reference['comp'] or ''}{reference['seq']}"
        raise StructureAnalysisError(f"Residue {label} was not found in this structure.")
    if reference["atom"]:
        named = [atom for atom in matched if atom.name == reference["atom"]]
        if not named:
            raise StructureAnalysisError(
                f"Atom {reference['atom']} was not found in residue {matched[0].residue_label}."
            )
        return named
    return matched


def _distance_metric(atoms: list[Atom], spec: dict[str, Any]) -> dict[str, Any]:
    scoped = first_model(atoms)
    left = _residue_atoms(scoped, spec["residues"][0])
    right = _residue_atoms(scoped, spec["residues"][1])

    nearest = min(
        ((a, b, _distance(a, b)) for a in left for b in right),
        key=lambda item: item[2],
    )
    rows = [
        {
            "measurement": "Closest approach",
            "value_A": round(nearest[2], 2),
            "detail": f"{nearest[0].name} … {nearest[1].name}",
        }
    ]

    def alpha_carbon(members: list[Atom]) -> Atom | None:
        return next((atom for atom in members if atom.name == "CA"), None)

    left_ca, right_ca = alpha_carbon(left), alpha_carbon(right)
    if left_ca and right_ca:
        rows.append(
            {
                "measurement": "Cα–Cα",
                "value_A": round(_distance(left_ca, right_ca), 2),
                "detail": "backbone separation",
            }
        )
    rows.append(
        {
            "measurement": "Centroid–centroid",
            "value_A": round(
                math.dist(_centroid(left), _centroid(right)),
                2,
            ),
            "detail": "all-atom centers",
        }
    )

    pocket = [
        (atom.chain, atom.seq)
        for atom in (left[0], right[0])
        if atom.chain and atom.seq is not None
    ]
    return {
        "headline": (
            f"{left[0].residue_label} … {right[0].residue_label}: "
            f"{nearest[2]:.2f} Å at closest approach"
        ),
        "columns": ["measurement", "value_A", "detail"],
        "rows": rows,
        "view_layers": (
            [_view_layer(residue_ranges=_residue_ranges(pocket), color_theme="residue-name")]
            if pocket
            else []
        ),
    }


def _centroid(atoms: list[Atom]) -> tuple[float, float, float]:
    count = len(atoms)
    return (
        sum(atom.x for atom in atoms) / count,
        sum(atom.y for atom in atoms) / count,
        sum(atom.z for atom in atoms) / count,
    )


def _contacts(atoms: list[Atom], spec: dict[str, Any]) -> dict[str, Any]:
    """Inter-chain residue contacts — the interface between two chains."""
    scoped = [atom for atom in first_model(atoms) if not atom.is_water]
    chains = spec["chains"]
    if len(chains) != 2:
        present = sorted({atom.chain for atom in scoped if atom.chain})
        raise StructureAnalysisError(
            'Contacts need exactly two chains, for example chains=["A", "B"]. '
            f"This structure has: {', '.join(present) or 'no named chains'}."
        )
    left = _selected(scoped, [chains[0]])
    right = _selected(scoped, [chains[1]])
    cutoff = spec["cutoff"]

    closest: dict[tuple[Any, Any], float] = {}
    for a, b, separation in atom_pairs_within(left, right, cutoff):
        key = (a.residue_key, b.residue_key)
        if key not in closest or separation < closest[key]:
            closest[key] = separation
    if not closest:
        raise StructureAnalysisError(
            f"Chains {chains[0]} and {chains[1]} have no atom pair within {cutoff:.1f} Å."
        )

    rows = []
    interface: list[tuple[str, int]] = []
    for (left_key, right_key), separation in sorted(closest.items(), key=lambda item: item[1]):
        _, left_chain, left_seq, left_ins, left_comp = left_key
        _, right_chain, right_seq, right_ins, right_comp = right_key
        rows.append(
            {
                "residue_a": f"{left_chain}:{left_comp}{left_seq or ''}{left_ins}",
                "residue_b": f"{right_chain}:{right_comp}{right_seq or ''}{right_ins}",
                "distance_A": round(separation, 2),
            }
        )
        for chain, seq in ((left_chain, left_seq), (right_chain, right_seq)):
            if chain and seq is not None:
                interface.append((chain, seq))

    return {
        "headline": (
            f"{len(rows)} residue-residue contacts between chains "
            f"{chains[0]} and {chains[1]} within {cutoff:.1f} Å"
        ),
        "columns": ["residue_a", "residue_b", "distance_A"],
        "rows": rows[:MAX_RESULT_ROWS],
        "view_layers": [
            _view_layer(residue_ranges=_residue_ranges(interface), color_theme="chain-id")
        ],
    }


def _polar_contacts(atoms: list[Atom], spec: dict[str, Any]) -> dict[str, Any]:
    """Polar contacts, salt bridges, and disulfides.

    Uploaded models rarely carry hydrogens, so N/O pairs inside the cutoff are
    reported as candidate polar contacts rather than as confirmed hydrogen
    bonds; geometry alone cannot settle the difference without donors.
    """
    scoped = [
        atom
        for atom in first_model(_selected(atoms, spec["chains"]))
        if not atom.is_water and atom.element in POLAR_ELEMENTS | {"S"}
    ]
    if not scoped:
        raise StructureAnalysisError("No polar (N, O, S) atoms were found in the selection.")
    cutoff = spec["cutoff"]

    rows: list[dict[str, Any]] = []
    involved: list[tuple[str, int]] = []
    for a, b, separation in atom_pairs_within(scoped, scoped, max(cutoff, _DISULFIDE_CUTOFF)):
        if a.residue_key == b.residue_key:
            continue
        # A self-search sees each pair from both ends. Keeping one fixed
        # ordering drops the mirror image without a set of every pair seen,
        # which would otherwise grow as large as the search itself.
        if (a.serial, a.name) > (b.serial, b.name):
            continue

        is_disulfide = (
            a.element == b.element == "S"
            and a.comp == b.comp == "CYS"
            and separation <= _DISULFIDE_CUTOFF
        )
        if a.element == "S" or b.element == "S":
            if not is_disulfide:
                continue
            kind = "disulfide"
        elif separation > cutoff:
            continue
        elif {(a.comp, a.name), (b.comp, b.name)} & _SALT_BRIDGE_ANIONS and {
            (a.comp, a.name),
            (b.comp, b.name),
        } & _SALT_BRIDGE_CATIONS:
            kind = "salt bridge"
        else:
            kind = "polar contact"

        rows.append(
            {
                "kind": kind,
                "atom_a": f"{a.residue_label}·{a.name}",
                "atom_b": f"{b.residue_label}·{b.name}",
                "distance_A": round(separation, 2),
            }
        )
        for atom in (a, b):
            if atom.chain and atom.seq is not None:
                involved.append((atom.chain, atom.seq))

    if not rows:
        raise StructureAnalysisError(
            f"No polar contact was found within {cutoff:.1f} Å in the selection."
        )
    rows.sort(
        key=lambda row: (
            row["kind"] != "disulfide",
            row["kind"] != "salt bridge",
            row["distance_A"],
        )
    )
    tally: dict[str, int] = {}
    for row in rows:
        tally[str(row["kind"])] = tally.get(str(row["kind"]), 0) + 1
    return {
        "headline": (
            ", ".join(f"{count} {kind}s" for kind, count in tally.items())
            + f" within {cutoff:.1f} Å (no hydrogens: candidates, not confirmed H-bonds)"
        ),
        "columns": ["kind", "atom_a", "atom_b", "distance_A"],
        "rows": rows[:MAX_RESULT_ROWS],
        "view_layers": [
            _view_layer(
                include="protein",
                residue_ranges=_residue_ranges(involved),
                color_theme="residue-name",
            )
        ],
    }


def _geometry(atoms: list[Atom], spec: dict[str, Any]) -> dict[str, Any]:
    scoped = [atom for atom in first_model(_selected(atoms, spec["chains"])) if not atom.is_water]
    if not scoped:
        raise StructureAnalysisError("The selection contains no non-water atoms.")
    masses = [_ATOMIC_MASS.get(atom.element, _DEFAULT_MASS) for atom in scoped]
    total = sum(masses)
    center = tuple(
        sum(mass * getattr(atom, axis) for atom, mass in zip(scoped, masses)) / total
        for axis in ("x", "y", "z")
    )
    gyration = math.sqrt(
        sum(
            mass * math.dist((atom.x, atom.y, atom.z), center) ** 2
            for atom, mass in zip(scoped, masses)
        )
        / total
    )
    extent = tuple(
        max(getattr(atom, axis) for atom in scoped) - min(getattr(atom, axis) for atom in scoped)
        for axis in ("x", "y", "z")
    )
    rows = [
        {"property": "Atoms measured", "value": f"{len(scoped):,}"},
        {"property": "Radius of gyration", "value": f"{gyration:.2f} Å"},
        {
            "property": "Center of mass",
            "value": f"({center[0]:.2f}, {center[1]:.2f}, {center[2]:.2f})",
        },
        {
            "property": "Bounding box",
            "value": f"{extent[0]:.1f} × {extent[1]:.1f} × {extent[2]:.1f} Å",
        },
        {"property": "Molecular weight (approx.)", "value": f"{total / 1000:.1f} kDa"},
    ]
    return {
        "headline": f"Radius of gyration {gyration:.2f} Å over {len(scoped):,} atoms",
        "columns": ["property", "value"],
        "rows": rows,
        "view_layers": [],
    }


def _looks_like_predicted_confidence(atoms: list[Atom]) -> bool:
    """Whether the B-factor column is really per-residue pLDDT.

    An experimental model refines a B-factor per atom, so values vary inside a
    residue. AlphaFold-style predictions write one confidence score to every
    atom of a residue, on a 0–100 scale — that pattern, not the range alone, is
    what distinguishes them.
    """
    per_residue: dict[tuple[Any, ...], tuple[set[float], int]] = {}
    for atom in atoms:
        if atom.b_factor is None:
            continue
        if not 0.0 <= atom.b_factor <= 100.0:
            return False
        values, count = per_residue.setdefault(atom.residue_key, (set(), 0))
        values.add(atom.b_factor)
        per_residue[atom.residue_key] = (values, count + 1)
    # Single-atom residues (ions, waters) are uniform by definition and would
    # make any structure look predicted.
    multi_atom = [values for values, count in per_residue.values() if count > 1]
    if len(multi_atom) < 5:
        return False
    uniform = sum(1 for values in multi_atom if len(values) == 1)
    return uniform / len(multi_atom) >= 0.95


def _quality(atoms: list[Atom], spec: dict[str, Any]) -> dict[str, Any]:
    """Model-quality signals that decide whether a region is worth analyzing."""
    scoped = first_model(_selected(atoms, spec["chains"]))
    polymer = [atom for atom in scoped if atom.is_polymer and not atom.is_water]
    b_factors = [atom.b_factor for atom in scoped if atom.b_factor is not None]
    occupancies = [atom.occupancy for atom in scoped if atom.occupancy is not None]
    altlocs = {atom.residue_key for atom in scoped if atom.alt}

    rows: list[dict[str, Any]] = []
    if b_factors:
        ordered = sorted(b_factors)
        median = ordered[len(ordered) // 2]
        predicted = _looks_like_predicted_confidence(scoped)
        rows.append(
            {
                "property": "pLDDT confidence" if predicted else "B-factor",
                "value": (
                    f"min {min(ordered):.1f}, median {median:.1f}, max {max(ordered):.1f}"
                    + ("  (constant per residue in 0–100: predicted model)" if predicted else "")
                ),
            }
        )
    if occupancies:
        partial = sum(1 for value in occupancies if value < 1.0)
        rows.append(
            {
                "property": "Partial occupancy atoms",
                "value": f"{partial:,} of {len(occupancies):,}",
            }
        )
    rows.append({"property": "Residues with alternate conformations", "value": f"{len(altlocs):,}"})

    gaps: list[str] = []
    by_chain: dict[str, set[int]] = {}
    for atom in polymer:
        if atom.chain and atom.seq is not None:
            by_chain.setdefault(atom.chain, set()).add(atom.seq)
    for chain, sequences in sorted(by_chain.items()):
        ordered = sorted(sequences)
        for previous, current in zip(ordered, ordered[1:]):
            if current > previous + 1:
                gaps.append(f"{chain}:{previous + 1}–{current - 1}")
    rows.append(
        {
            "property": "Chain breaks (unmodeled residues)",
            "value": ", ".join(gaps[:12]) + ("…" if len(gaps) > 12 else "") if gaps else "none",
        }
    )
    return {
        "headline": (
            f"{len(gaps)} chain break(s), {len(altlocs):,} residues with alternate conformations"
        ),
        "columns": ["property", "value"],
        "rows": rows,
        "view_layers": [],
    }


_METRICS = {
    "summary": _summary,
    "ligands": _ligands,
    "binding_site": _binding_site,
    "distance": _distance_metric,
    "contacts": _contacts,
    "polar_contacts": _polar_contacts,
    "geometry": _geometry,
    "quality": _quality,
}


def measure(data: bytes | str, spec: Any) -> dict[str, Any]:
    """Run one validated measurement over an uploaded structure.

    Returns a headline, a table (`columns` + `rows`), and the view layers that
    show what was measured, so a number and the picture behind it stay in sync.
    """
    safe = normalize_metric_spec(spec)
    if safe is None:
        raise StructureAnalysisError(
            "The measurement request is invalid. Check the metric name, chains, "
            "ligand, cutoff, and residues."
        )
    result = _METRICS[safe["metric"]](parse_atoms(data), safe)
    result["metric"] = safe["metric"]
    result["spec"] = safe
    return result


# --------------------------------------------------------------------------
# Agent protocol
# --------------------------------------------------------------------------


def _marked_json_values(value: str) -> Iterator[tuple[int, int, Any]]:
    decoder = json.JSONDecoder()
    cursor = 0
    while (marker_at := value.find(STRUCTURE_METRIC_MARKER, cursor)) >= 0:
        payload_start = marker_at + len(STRUCTURE_METRIC_MARKER)
        tail = value[payload_start:]
        json_start = payload_start + len(tail) - len(tail.lstrip())
        try:
            candidate, consumed = decoder.raw_decode(value[json_start:])
        except json.JSONDecodeError:
            cursor = payload_start
            continue
        end = json_start + consumed
        yield marker_at, end, candidate
        cursor = end


def _walk(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def metric_specs_from_result(result: Any) -> list[dict[str, Any]]:
    """Extract unique, validated measurement requests from nested agent output."""
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
            spec = normalize_metric_spec(candidate)
            if spec is None:
                continue
            fingerprint = json.dumps(spec, sort_keys=True)
            if fingerprint not in seen:
                seen.add(fingerprint)
                specs.append(spec)
    return specs


def metric_specs_for_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    specs = metric_specs_from_result(state.get("answer", ""))
    for tool in state.get("tools", []):
        normalized_name = str(tool.get("name", "")).rsplit("___", 1)[-1]
        if normalized_name in {"code_interpreter", "python"} and tool.get("result") is not None:
            specs.extend(metric_specs_from_result(tool["result"]))
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for spec in specs:
        fingerprint = json.dumps(spec, sort_keys=True)
        if fingerprint not in seen:
            seen.add(fingerprint)
            unique.append(spec)
    return unique


def response_without_metric_payloads(response: str) -> str:
    """Remove valid machine-readable measurement requests from user-facing prose."""
    spans = [
        (start, end)
        for start, end, value in _marked_json_values(response)
        if normalize_metric_spec(value)
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


def metric_description(spec: dict[str, Any]) -> str:
    """Human-readable label for a validated measurement request."""
    parts = [spec["metric"].replace("_", " ")]
    if spec["ligand"]:
        parts.append(spec["ligand"])
    if spec["chains"]:
        parts.append("chain " + ", ".join(spec["chains"]))
    if spec["residues"]:
        parts.append(
            " ↔ ".join(
                f"{item['chain'] or '?'}:{item['comp']}{item['seq']}" for item in spec["residues"]
            )
        )
    if spec["metric"] in {"binding_site", "contacts", "polar_contacts"}:
        parts.append(f"{spec['cutoff']:.1f} Å")
    return " · ".join(parts)
