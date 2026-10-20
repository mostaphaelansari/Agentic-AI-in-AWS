"""Load Streamlit styles from the asset directory."""

from pathlib import Path

import streamlit as st

STYLE_PATH = Path(__file__).resolve().parents[1] / "assets" / "styles.css"


def inject_styles() -> None:
    st.markdown(f"<style>{STYLE_PATH.read_text()}</style>", unsafe_allow_html=True)
