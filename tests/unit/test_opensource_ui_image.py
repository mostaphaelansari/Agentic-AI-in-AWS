"""Checks for shared modules required by the open-source UI image."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UI_DOCKERFILE = ROOT / "opensource" / "ui" / "Dockerfile"
UI_DOCKERIGNORE = ROOT / "opensource" / "ui" / "Dockerfile.dockerignore"


def test_ui_image_includes_shared_molstar_modules() -> None:
    dockerfile = UI_DOCKERFILE.read_text()
    dockerignore = UI_DOCKERIGNORE.read_text()

    for filename in ("molecule_viewer.py", "molstar_component.py"):
        shared_path = f"apps/comparison/{filename}"
        assert f"COPY {shared_path} {shared_path}" in dockerfile
        assert f"!{shared_path}" in dockerignore
