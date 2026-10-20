"""Lazy, route-only adapter for the optional ulab-uiuc/LLMRouter package."""

from __future__ import annotations

import importlib
import os
import pickle
from functools import lru_cache
from pathlib import Path
from threading import Lock
from typing import Any


class LLMRouterError(RuntimeError):
    """Raised when LLMRouter cannot produce a usable model selection."""


_LOAD_LOCK = Lock()


class _KNNRouteOnly:
    """Minimal KNN predictor that avoids importing every LLMRouter strategy."""

    def __init__(self, model: Any, embed: Any) -> None:
        self._model = model
        self._embed = embed

    def route(self, prompt: str) -> str:
        vector = self._embed(prompt).numpy()
        selected = self._model.predict([vector])[0]
        return str(selected)


class _LongformerEmbedding:
    """Exact LLMRouter embedding without importing its heavyweight utils package."""

    _MODEL_NAME = "allenai/longformer-base-4096"

    def __init__(self, torch: Any, transformers: Any) -> None:
        self._torch = torch
        self._transformers = transformers
        self._tokenizer: Any = None
        self._model: Any = None
        self._lock = Lock()

    def _initialize(self) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            local_only = os.environ.get("LLMROUTER_LOCAL_FILES_ONLY", "true").lower() != "false"
            self._tokenizer = self._transformers.AutoTokenizer.from_pretrained(
                self._MODEL_NAME,
                local_files_only=local_only,
            )
            self._model = self._transformers.AutoModel.from_pretrained(
                self._MODEL_NAME,
                local_files_only=local_only,
            )
            self._model.eval()

    def __call__(self, prompt: str) -> Any:
        self._initialize()
        inputs = self._tokenizer(
            [prompt],
            padding=True,
            truncation=True,
            max_length=4096,
            return_tensors="pt",
        )
        with self._torch.inference_mode():
            hidden = self._model(**inputs).last_hidden_state
            mask = inputs["attention_mask"].unsqueeze(-1).expand(hidden.size()).float()
            pooled = (hidden * mask).sum(1) / mask.sum(1)
        return pooled[0].cpu()


def _load_knn_router(config_path: str) -> _KNNRouteOnly:
    """Load only the artifacts needed for KNN route-only inference."""
    try:
        yaml = importlib.import_module("yaml")
        torch = importlib.import_module("torch")
        transformers = importlib.import_module("transformers")
    except (ImportError, ModuleNotFoundError) as exc:
        raise LLMRouterError("LLMRouter is not installed; run `uv sync --extra llmrouter`") from exc

    try:
        threads = max(1, int(os.environ.get("LLMROUTER_TORCH_THREADS", "2")))
        torch.set_num_threads(threads)
        with Path(config_path).open(encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
        configured_path = config["model_path"]["load_model_path"]
        model_path = Path(configured_path).expanduser()
        if not model_path.is_absolute():
            model_path = Path(config_path).parent / model_path
        with model_path.open("rb") as handle:
            model = pickle.load(handle)
    except (KeyError, OSError, TypeError, ValueError, pickle.UnpicklingError) as exc:
        raise LLMRouterError("LLMRouter could not load its configured KNN model") from exc
    if not callable(getattr(model, "predict", None)):
        raise LLMRouterError("LLMRouter KNN artifact does not expose predict()")
    return _KNNRouteOnly(model, _LongformerEmbedding(torch, transformers))


@lru_cache(maxsize=4)
def _load_router(router_name: str, config_path: str) -> tuple[Any, Any]:
    """Load one configured router and retain it across application requests."""
    if router_name == "knnrouter":
        return None, _load_knn_router(config_path)

    try:
        inference = importlib.import_module("llmrouter.cli.router_inference")
    except (ImportError, ModuleNotFoundError) as exc:
        raise LLMRouterError("LLMRouter is not installed; run `uv sync --extra llmrouter`") from exc

    try:
        router = inference.load_router(router_name, config_path)
    except Exception as exc:
        raise LLMRouterError("LLMRouter could not load its configured router") from exc
    return inference, router


def route_model_name(prompt: str, router_name: str, config_path: str) -> str:
    """Return LLMRouter's route-only model name without invoking that model."""
    normalized_name = router_name.strip().lower()
    if not normalized_name:
        raise LLMRouterError("LLMRouter router name is empty")

    path = Path(config_path).expanduser()
    if not config_path.strip() or not path.is_file():
        raise LLMRouterError("LLMRouter config path is missing or is not a file")

    # functools.lru_cache may invoke the wrapped function more than once during
    # concurrent first use. Model loading can be expensive, so serialize misses.
    cache_key = str(path.resolve())
    with _LOAD_LOCK:
        inference, router = _load_router(normalized_name, cache_key)

    if inference is None:
        try:
            return router.route(prompt)
        except Exception as exc:
            raise LLMRouterError("LLMRouter route-only inference failed") from exc

    try:
        result = inference.route_query(prompt, router, normalized_name)
    except Exception as exc:
        raise LLMRouterError("LLMRouter route-only inference failed") from exc

    if not isinstance(result, dict) or result.get("success") is not True:
        raise LLMRouterError("LLMRouter did not return a successful route")
    model_name = result.get("model_name")
    if not isinstance(model_name, str) or not model_name.strip():
        raise LLMRouterError("LLMRouter did not return a model name")
    return model_name.strip()
