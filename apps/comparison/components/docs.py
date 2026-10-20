"""Documentation view for the Streamlit comparison app."""

from __future__ import annotations

import html
import re
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

ROOT = Path(__file__).resolve().parents[3]
DOCS_DIR = ROOT / "docs"
MERMAID_BLOCK = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _document_label(path: Path) -> str:
    if path.name == "README.md":
        return "README"
    relative = path.relative_to(DOCS_DIR).with_suffix("")
    return str(relative).replace("/", " / ").replace("-", " ").replace("_", " ").title()


def _documents() -> dict[str, Path]:
    docs = {"README": ROOT / "README.md"}
    for path in sorted(DOCS_DIR.rglob("*.md")):
        docs[_document_label(path)] = path
    return docs


def render_docs_selector() -> str:
    """Render the sidebar document selector and return the selected label."""

    documents = _documents()
    return st.selectbox("Document", list(documents))


def _render_mermaid(source: str) -> None:
    diagram = html.escape(source.strip())
    components.html(
        f"""
        <div class="mermaid">{diagram}</div>
        <script type="module">
          import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
          mermaid.initialize({{
            startOnLoad: false,
            securityLevel: "loose",
            theme: "default"
          }});
          await mermaid.run({{ querySelector: ".mermaid" }});
        </script>
        """,
        height=560,
        scrolling=True,
    )


def _render_markdown_with_mermaid(markdown: str) -> None:
    cursor = 0
    for match in MERMAID_BLOCK.finditer(markdown):
        before = markdown[cursor : match.start()]
        if before.strip():
            st.markdown(before)
        _render_mermaid(match.group(1))
        cursor = match.end()

    after = markdown[cursor:]
    if after.strip():
        st.markdown(after)


def render_docs_view(selected: str) -> None:
    """Render one project documentation file."""

    documents = _documents()
    path = documents[selected]

    if not path.exists():
        st.error(f"Documentation file not found: {path.relative_to(ROOT)}")
        return

    st.subheader(selected)
    _render_markdown_with_mermaid(path.read_text())
