"""Tests for the lazy route-only LLMRouter adapter."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

APP_ROOT = Path(__file__).resolve().parents[2] / "apps" / "comparison"
sys.path.insert(0, str(APP_ROOT))

from services import llmrouter_adapter  # noqa: E402


@pytest.fixture(autouse=True)
def clear_router_cache():
    llmrouter_adapter._load_router.cache_clear()
    yield
    llmrouter_adapter._load_router.cache_clear()


def test_route_model_name_loads_once_and_uses_route_only_api(tmp_path, monkeypatch) -> None:
    config = tmp_path / "router.yaml"
    config.write_text("hparam: {}\n", encoding="utf-8")
    calls: list[tuple] = []
    router = object()

    fake_inference = SimpleNamespace(
        load_router=lambda name, path: calls.append(("load", name, path)) or router,
        route_query=lambda prompt, loaded, name: (
            calls.append(("route", prompt, loaded, name))
            or {"success": True, "model_name": "economy"}
        ),
    )
    monkeypatch.setattr(
        llmrouter_adapter.importlib,
        "import_module",
        lambda name: calls.append(("import", name)) or fake_inference,
    )
    first = llmrouter_adapter.route_model_name("hello", "SVMRouter", str(config))
    second = llmrouter_adapter.route_model_name("again", "SVMRouter", str(config))

    assert first == second == "economy"
    assert [call[0] for call in calls].count("load") == 1
    assert [call[0] for call in calls].count("route") == 2


def test_knn_uses_minimal_route_only_loader(tmp_path, monkeypatch) -> None:
    model_path = tmp_path / "knn.pkl"
    model_path.write_bytes(b"placeholder")
    config = tmp_path / "router.yaml"
    config.write_text(
        f'model_path:\n  load_model_path: "{model_path}"\n',
        encoding="utf-8",
    )
    imports: list[str] = []
    thread_counts: list[int] = []
    model = SimpleNamespace(predict=lambda _vectors: ["strong"])
    vector = SimpleNamespace(numpy=lambda: [0.1, 0.2])
    real_import = llmrouter_adapter.importlib.import_module

    def fake_import(name: str):
        imports.append(name)
        if name == "torch":
            return SimpleNamespace(set_num_threads=thread_counts.append)
        if name == "transformers":
            return SimpleNamespace()
        return real_import(name)

    monkeypatch.setattr(llmrouter_adapter.importlib, "import_module", fake_import)
    monkeypatch.setattr(llmrouter_adapter.pickle, "load", lambda _handle: model)
    monkeypatch.setattr(
        llmrouter_adapter,
        "_LongformerEmbedding",
        lambda _torch, _transformers: lambda _prompt: vector,
    )

    selected = llmrouter_adapter.route_model_name("analyze it", "KNNRouter", str(config))

    assert selected == "strong"
    assert thread_counts == [2]
    assert "llmrouter.cli.router_inference" not in imports
    assert "llmrouter.utils.embeddings" not in imports


def test_route_model_name_requires_a_real_config_path() -> None:
    with pytest.raises(llmrouter_adapter.LLMRouterError, match="config path"):
        llmrouter_adapter.route_model_name("hello", "knnrouter", "")


def test_route_model_name_rejects_failed_results(tmp_path, monkeypatch) -> None:
    config = tmp_path / "router.yaml"
    config.write_text("hparam: {}\n", encoding="utf-8")
    fake_inference = SimpleNamespace(
        load_router=lambda _name, _path: object(),
        route_query=lambda *_args: {"success": False, "error": "bad route"},
    )
    monkeypatch.setattr(llmrouter_adapter.importlib, "import_module", lambda _name: fake_inference)
    with pytest.raises(llmrouter_adapter.LLMRouterError, match="successful route"):
        llmrouter_adapter.route_model_name("hello", "svmrouter", str(config))
