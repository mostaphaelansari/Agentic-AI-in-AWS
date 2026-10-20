"""Background lifecycle for one Managed-versus-Custom comparison run."""

from __future__ import annotations

import concurrent.futures
import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable

from backends.common import get_client
from backends.dispatcher import dispatch
from config import ARCHITECTURES
from models.state import new_comparison_state, snapshot_states

from services.event_reducer import apply_event, finish_state
from services.model_router import routed_options

logger = logging.getLogger("unified-ui.run-controller")

ACTIVE_RUN_KEY = "active_comparison_run_id"
STOP_SETTLE_SECONDS = 1.0


class _RunQueue(queue.Queue):
    """Drop backend events once their run has been frozen and committed."""

    def __init__(self, controller: RunController):
        super().__init__()
        self._controller = controller

    def put(self, item, block: bool = True, timeout: float | None = None) -> None:
        with self._controller.lock:
            if self._controller.finalized:
                return
        super().put(item, block=block, timeout=timeout)


class RunController:
    """Own worker threads and mutable state independently of Streamlit reruns."""

    def __init__(
        self,
        prompt: str,
        options: dict,
        *,
        display_prompt: str | None = None,
        agent_prompts: dict[str, str] | None = None,
        parent_id: str | None = None,
        branch_source_id: str | None = None,
        ledger_id: str | None = None,
        cleanup: Callable[[], None] | None = None,
        architectures: tuple[str, ...] | None = None,
        page_scope: str = "comparison",
    ) -> None:
        self.run_id = f"comparison-{uuid.uuid4().hex}"
        self.prompt = display_prompt or prompt
        self.agent_prompt = prompt
        self.agent_prompts = dict(agent_prompts or {})
        self.parent_id = parent_id
        self.branch_source_id = branch_source_id
        self.ledger_id = ledger_id
        self.page_scope = page_scope
        self.options = dict(options)
        selected_architectures = architectures or tuple(ARCHITECTURES)
        unknown = [name for name in selected_architectures if name not in ARCHITECTURES]
        if unknown:
            raise ValueError(f"Unknown architecture(s): {', '.join(unknown)}")
        if not selected_architectures:
            raise ValueError("At least one architecture must be selected")
        self.states = {
            name: new_comparison_state(name, ARCHITECTURES[name])
            for name in selected_architectures
        }
        self.lock = threading.RLock()
        self.cancelled = threading.Event()
        self.queues = {name: _RunQueue(self) for name in self.states}
        self.stream_handles: dict[str, object] = {}
        self.futures: dict[str, concurrent.futures.Future] = {}
        self.executor: concurrent.futures.ThreadPoolExecutor | None = None
        self.finalized = False
        self.committed = False
        self._committing = False
        self._stopped_at: float | None = None
        self._cancelled_arches: set[str] = set()
        self._cleanup = cleanup
        self._cleaned_up = False

    def start(self) -> None:
        """Route and submit the selected architectures without blocking Streamlit."""
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=len(self.states) + 1,
            thread_name_prefix=f"comparison-{self.run_id[-8:]}",
        )
        with self.lock:
            self.executor = executor
            for state in self.states.values():
                state["status"] = "Routing"
            future = executor.submit(self._route_and_dispatch)
            self.futures["_router"] = future
            future.add_done_callback(self._cleanup_after_workers)

    def _route_and_dispatch(self) -> None:
        """Select one sticky model, then launch the architecture workers."""
        try:
            # Classify the user's request, not transport/tool instructions added
            # later for an uploaded file. Those instructions can dominate the
            # embedding and move an otherwise strong request into a cheap KNN
            # neighborhood. Workers still receive the complete agent prompt.
            routed = routed_options(self.prompt, self.options)
        except Exception as exc:
            logger.exception("model routing failed run_id=%s", self.run_id)
            with self.lock:
                for state in self.states.values():
                    state["errors"].append(str(exc))
                    finish_state(state, status="Error")
            return

        with self.lock:
            self.options = routed
            if self.cancelled.is_set() or self.finalized or self.executor is None:
                return
            router_metadata = {
                key: routed[key]
                for key in (
                    "model_router_mode",
                    "model_router_name",
                    "model_router_version",
                    "model_router_candidate",
                    "model_router_outcome",
                    "model_router_fallback",
                    "model_router_latency_ms",
                    "model_router_classifier_model",
                    "model_router_classifier_path",
                    "model_router_classifier_tier",
                )
                if key in routed
            }
            for arch_name, state in self.states.items():
                state["status"] = "Waiting"
                # Routing is performed once before fan-out. Copy its metadata to
                # both result states so the shared overhead remains visible in
                # live cards, history snapshots, and the session ledger.
                state["metrics"] = {**state.get("metrics", {}), **router_metadata}
                future = self.executor.submit(
                    dispatch,
                    state["config"],
                    state["session_id"],
                    self.agent_prompts.get(arch_name, self.agent_prompt),
                    self.queues[arch_name],
                    self.options,
                    controller=self,
                    arch_name=arch_name,
                )
                self.futures[arch_name] = future
                future.add_done_callback(self._cleanup_after_workers)

    def _cleanup_after_workers(self, _future: concurrent.futures.Future) -> None:
        if self._workers_done():
            self.cleanup()

    def register_stream(self, arch_name: str, handle: object) -> None:
        """Expose a blocking botocore stream so Stop can close it cross-thread."""
        with self.lock:
            if self.finalized:
                close_now = True
            else:
                self.stream_handles[arch_name] = handle
                close_now = self.cancelled.is_set()
            service = self.states[arch_name]["config"].get("service")
        if close_now:
            self._close_handle(arch_name, handle)
            if service == "runtime":
                self._start_runtime_stop(arch_name)

    @staticmethod
    def _close_handle(arch_name: str, handle: object) -> None:
        close = getattr(handle, "close", None)
        if close is None:
            return
        try:
            close()
        except Exception:
            logger.debug("stream close failed arch=%s", arch_name, exc_info=True)

    def _stop_runtime_session(self, arch_name: str) -> None:
        state = self.states[arch_name]
        config = state["config"]
        try:
            response = get_client(config["region"]).stop_runtime_session(
                agentRuntimeArn=config["arn"],
                runtimeSessionId=state["session_id"],
            )
            logger.info(
                "runtime session stopped arch=%s session_id=%s status=%s",
                arch_name,
                state["session_id"],
                response.get("statusCode"),
            )
        except Exception:
            # Closing the response stream still triggers the in-container
            # Agent.cancel() fallback when the platform propagates disconnect.
            logger.warning(
                "runtime session stop failed arch=%s session_id=%s",
                arch_name,
                state["session_id"],
                exc_info=True,
            )

    def _start_runtime_stop(self, arch_name: str) -> None:
        threading.Thread(
            target=self._stop_runtime_session,
            args=(arch_name,),
            name=f"stop-{self.run_id[-8:]}",
            daemon=True,
        ).start()

    def _terminate(self, handles: list[tuple[str, object]], runtime_arches: list[str]) -> None:
        for arch_name, handle in handles:
            self._close_handle(arch_name, handle)
        for arch_name in runtime_arches:
            self._stop_runtime_session(arch_name)

    def stop(self) -> None:
        """Request cancellation and return without joining blocking workers."""
        with self.lock:
            if self.finalized or self.cancelled.is_set():
                return
            self.cancelled.set()
            self._stopped_at = time.monotonic()
            for state in self.states.values():
                if not state["done"]:
                    state["status"] = "Stopping"
                    self._cancelled_arches.add(state["arch_name"])
            handles = list(self.stream_handles.items())
            runtime_arches = [
                name
                for name, state in self.states.items()
                if state["config"].get("service") == "runtime"
            ]
        threading.Thread(
            target=self._terminate,
            args=(handles, runtime_arches),
            name=f"terminate-{self.run_id[-8:]}",
            daemon=True,
        ).start()

    def drain(self) -> bool:
        """Apply every queued backend event and report whether state changed."""
        changed = False
        with self.lock:
            if self.finalized:
                return False
            for arch_name, event_queue in self.queues.items():
                while True:
                    try:
                        item = event_queue.get_nowait()
                    except queue.Empty:
                        break
                    apply_event(self.states[arch_name], item)
                    changed = True
        return changed

    def _workers_done(self) -> bool:
        with self.lock:
            return bool(self.futures) and all(future.done() for future in self.futures.values())

    def ready_to_finalize(self) -> bool:
        self.drain()
        with self.lock:
            if self.finalized or all(state["done"] for state in self.states.values()):
                return True
            stopped_at = self._stopped_at
        return self._workers_done() or bool(
            self.cancelled.is_set()
            and stopped_at is not None
            and time.monotonic() - stopped_at >= STOP_SETTLE_SECONDS
        )

    def finalize(self) -> dict[str, dict]:
        """Freeze one terminal snapshot; safe to call more than once."""
        self.drain()
        with self.lock:
            if not self.finalized:
                now = time.time()
                for arch_name, state in self.states.items():
                    if arch_name in self._cancelled_arches:
                        finish_state(state, status="Stopped")
                    elif not state["done"]:
                        finish_state(state, status="Stopped")
                    if not state.get("elapsed"):
                        state["elapsed"] = max(now - float(state["started_at"]), 0.0)
                self.finalized = True
                if self.executor is not None:
                    self.executor.shutdown(wait=False, cancel_futures=True)
            return snapshot_states(self.states)

    def snapshot(self) -> dict[str, dict]:
        with self.lock:
            return snapshot_states(self.states)

    def commit_once(self, callback: Callable[[dict[str, dict]], None]) -> bool:
        """Run the Streamlit history/ledger mutation once, retrying after failure."""
        with self.lock:
            if not self.finalized or self.committed or self._committing:
                return False
            self._committing = True
            states = snapshot_states(self.states)
        try:
            callback(states)
        except Exception:
            with self.lock:
                self._committing = False
            raise
        with self.lock:
            self.committed = True
            self._committing = False
        self.cleanup()
        return True

    def cleanup(self) -> None:
        with self.lock:
            if self._cleaned_up:
                return
            self._cleaned_up = True
            callback = self._cleanup
        if callback is not None:
            try:
                callback()
            except Exception:
                logger.warning("run cleanup failed run_id=%s", self.run_id, exc_info=True)


_RUNS_LOCK = threading.Lock()
RUNS: dict[str, RunController] = {}


def register_run(controller: RunController) -> RunController:
    with _RUNS_LOCK:
        RUNS[controller.run_id] = controller
    return controller


def get_run(run_id: str | None) -> RunController | None:
    if not run_id:
        return None
    with _RUNS_LOCK:
        return RUNS.get(run_id)


def drop_run(run_id: str) -> None:
    with _RUNS_LOCK:
        RUNS.pop(run_id, None)
