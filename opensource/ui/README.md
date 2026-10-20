# Open-source multi-workspace UI

Streamlit front end for the local custom agent (`opensource/agents/custom`). It
opens on an **Agent Catalog** inspired by scientific workflow libraries: a
branded shell, persistent left navigation, searchable agent cards, capability
tags, and direct workspace launch buttons. The sidebar exposes two independent
prompt pages:

- **Travel Agent** for destination research, flights, stays, forecasts, maps,
  web research, and optional CSV analysis.
- **Protein Analysis** for mmCIF questions, residue selection, analysis charts,
  interactive Mol* 3D views, and a live structure notebook.

Each page has its own prompt field, agent session, examples, upload widget, run
history, and current result. Switching pages does not mix travel and molecular
conversation context.

The Travel Agent page accepts an optional UTF-8 CSV file. The Protein Analysis
page accepts one UTF-8 mmCIF (`.cif`, `.mcif`, or `.mmcif`) file. Uploads are
limited to 1 MB and staged in the isolated per-chat code-interpreter workspace.
Analysis prompts can return compact Vega-Lite charts. Protein uploads also open
an embedded Mol* viewer with rotation, zoom, selection, representation, and
screenshot controls. Display prompts such as “show only protein chain A,
residues 10–30” produce a filtered Mol* view through a validated data-only
selection protocol.

The protein workspace is a two-pane page: the upload widget, prompt, and the
agent's streaming analysis stay in the left pane, while the right pane holds the
Mol* view and stays pinned as the analysis scrolls. Uploading a structure does
not render it — the 3D view appears with the agent's answer, so what you see is
always the view the agent asked for.

After an answer creates the 3D result, the left pane also exposes a Jupyter-style
Python cell. Running `view(...)` replaces the live view, `add_view(...)` unions a
new molecular selection into it, and `reset_view()` restores the full answered
structure. A cell can select chains, residue names and ranges, water, molecular
categories, and the Mol* color theme. The update is applied immediately to the
right pane and remains available to subsequent agent prompts.

The notebook intentionally parses this small declarative API instead of running
arbitrary Python. This gives the workflow notebook ergonomics without turning
the public Streamlit UI into an unrestricted code-execution endpoint; the
isolated agent code interpreter remains the place for general data analysis.

## Structure measurements

The 3D view says what a structure looks like; the **Structure analysis** panel
says what its numbers are. It computes, from the uploaded coordinates and with
the standard library only:

- `summary` — chains, polymer residues, ligand atoms, waters;
- `ligands` — every non-water ligand copy with its elements;
- `binding_site` — polymer residues lining a ligand pocket, ranked by closest
  approach, with the atom pair that makes each contact;
- `distance` — closest approach, Cα–Cα, and centroid separation between two
  residues, optionally between two named atoms;
- `contacts` — the residue-residue interface between two chains;
- `polar_contacts` — candidate polar contacts, salt bridges, and disulfides;
- `geometry` — radius of gyration, center of mass, bounding box, mass;
- `quality` — B-factor or pLDDT range, partial occupancy, alternate
  conformations, and unmodeled chain breaks.

A measurement also returns the view layers that show exactly the residues it
measured, so the number and the picture cannot drift apart. Run one from the
panel buttons, from a `measure(...)` notebook cell, or let the agent request one
by emitting a `__STRUCTURE_METRIC__:` object — the arithmetic always happens in
trusted Python over the real file, never in the model's head.

Two honest limits: uploaded models rarely carry hydrogens, so polar contacts are
reported as candidates rather than confirmed hydrogen bonds, and per-residue
solvent-accessible surface area is not computed.

Display requests compose across turns. “Show protein residues 10–30” then “add
the iron” keeps the residues and unions the iron on top, because the agent marks
the second request as additive and the UI stacks view layers rather than
replacing them. A fresh “show me X” starts over, a question with no display
request leaves the view alone, and **Reset view** returns to the full structure.

Mol* is mounted through a bidirectional Streamlit Components v2 wrapper:
clicking residues in the answer's view records a bounded selection that is added
to the next submitted prompt. The full structure remains visible when the agent
does not request a usable filter, and in-memory run history retains the
structure across Streamlit reruns and history selection.

Run:

```
uv run streamlit run opensource/ui/app.py
```

Completed answers expose thumbs-up/down feedback when the agent reports an
MLflow trace ID. The UI sends the vote to the agent's `POST /feedback`
endpoint, which follows the same API-key policy as `/invocations` and attaches
a `user_satisfaction` human assessment to that exact trace. Votes therefore
appear beside automated judge scores in the bundled MLflow UI rather than in a
separate UI database.

## Session behavior

Login (when `opensource/ui/auth/users.json` has entries - see `auth.py`) is
held only in Streamlit's in-memory `st.session_state`:

- Survives reruns and normal page interaction within the same browser tab.
- Does **not** survive a full page refresh or browser restart - Streamlit
  gives each new browser session a fresh `session_state`, so the login form
  reappears.
- There is no "remember me" option. Adding one securely would require a
  signed, expiring session token stored in a cookie; the current stdlib-only,
  no-external-service architecture does not have a cookie/session layer, so
  this has been intentionally left out rather than persisting anything
  insecure (e.g. a reusable password hash) in browser-accessible state.

## Stylesheet

Styles live in `opensource/ui/assets/styles.css` and are self-contained -
they do not depend on `apps/comparison`'s stylesheet or class names.
