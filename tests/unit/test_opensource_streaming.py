from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
SERVICE_DIR = ROOT / "opensource" / "agents" / "custom"
UI_PATH = ROOT / "opensource" / "ui" / "app.py"

sys.path.insert(0, str(SERVICE_DIR))
import service  # noqa: E402


def _load_ui_module():
    spec = importlib.util.spec_from_file_location("opensource_ui_app", UI_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _decode_sse(chunks: list[str]) -> list[dict]:
    events = []
    for chunk in chunks:
        assert chunk.startswith("data: ")
        events.append(json.loads(chunk.removeprefix("data: ").strip()))
    return events


def test_service_streams_text_tool_trace_and_metrics(monkeypatch):
    class FakeMetrics:
        accumulated_usage = {
            "inputTokens": 11,
            "outputTokens": 7,
            "totalTokens": 18,
        }
        cycle_count = 2

    class FakeResult:
        metrics = FakeMetrics()

    class FakeAgent:
        def __init__(self, tool_hook):
            self.tool_hook = tool_hook

        async def stream_async(self, prompt):
            yield {"data": "Checking "}
            tool_use = {
                "toolUseId": "tool-1",
                "name": "searchLocation",
                "input": {"query": "Antibes"},
            }
            yield {"current_tool_use": tool_use}
            self.tool_hook._on_before(SimpleNamespace(tool_use=tool_use))
            self.tool_hook._on_after(
                SimpleNamespace(tool_use=tool_use, result={"content": "Antibes, France"})
            )
            yield {"data": "The weather is mild."}
            yield {"result": FakeResult()}

    monkeypatch.setattr(service, "_start_mcp_client", lambda: (None, None))
    monkeypatch.setattr(
        service, "_build_agent", lambda tool_hook, *args, **kwargs: FakeAgent(tool_hook)
    )

    async def run_inline(function, *args):
        return function(*args)

    monkeypatch.setattr(service.asyncio, "to_thread", run_inline)

    async def collect():
        return [chunk async for chunk in service._event_stream("Weather in Antibes?")]

    events = _decode_sse(asyncio.run(collect()))

    # tool_start appears twice on purpose: the stream-side event fires as soon
    # as the tool use starts, the hook-side one carries the final input; the
    # UI merges them by tool_use_id (covered by the reducer test below).
    assert [event["type"] for event in events] == [
        "text",
        "tool_start",
        "tool_start",
        "tool_end",
        "text",
        "metrics",
    ]
    assert events[1]["tool_use_id"] == "tool-1"
    assert events[2]["tool_use_id"] == "tool-1"
    assert events[3]["result"] == "Antibes, France"
    assert events[5]["tool_calls"] == 1
    assert events[5]["usage"]["totalTokens"] == 18


def test_service_forwards_session_id_to_agent(monkeypatch):
    captured = {}

    class FakeAgent:
        async def stream_async(self, prompt):
            yield {"data": "hi"}

    def fake_build(tool_hook, agent_tools=None, **kwargs):
        captured.update(kwargs)
        return FakeAgent()

    monkeypatch.setattr(service, "_start_mcp_client", lambda: (None, None))
    monkeypatch.setattr(service, "_build_agent", fake_build)

    async def run_inline(function, *args):
        return function(*args)

    monkeypatch.setattr(service.asyncio, "to_thread", run_inline)

    async def collect():
        return [c async for c in service._event_stream("hi", "user-1", "sess-42")]

    asyncio.run(collect())
    assert captured["session_id"] == "sess-42"
    assert isinstance(captured["model_dispatch_timer"], service.ModelDispatchTimer)


def test_model_dispatch_timer_records_only_the_first_call(monkeypatch):
    ticks = iter((10.125, 11.0))
    monkeypatch.setattr(service, "MLFLOW_ENABLED", False)
    monkeypatch.setattr(service.time, "perf_counter", lambda: next(ticks))
    timer = service.ModelDispatchTimer(10.0)

    timer.mark(provider="litellm-auto", model_id="opensource-auto-router")
    timer.mark(provider="litellm-auto", model_id="opensource-auto-router")

    assert timer.elapsed_ms == 125.0


def test_lifespan_initializes_litellm_before_readiness(monkeypatch):
    events = []
    monkeypatch.setattr(service, "MODEL_PROVIDER", "litellm")
    monkeypatch.setattr(service, "OLLAMA_WARMUP_ENABLED", False)
    monkeypatch.setattr(service, "_litellm_router", lambda: events.append("router"))

    async def run_inline(function, *args):
        return function(*args)

    monkeypatch.setattr(service.asyncio, "to_thread", run_inline)

    async def run_lifespan():
        async with service._lifespan(service.app):
            events.append("ready")

    asyncio.run(run_lifespan())
    assert events == ["router", "ready"]


def test_optional_ollama_warmup_loads_model_without_routing(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, host):
            captured["host"] = host

        async def generate(self, **kwargs):
            captured["request"] = kwargs

    monkeypatch.setattr(service.ollama, "AsyncClient", FakeClient)
    asyncio.run(service._warm_ollama_model())

    assert captured["host"] == service.OLLAMA_HOST
    assert captured["request"] == {
        "model": service.OLLAMA_MODEL_ID,
        "prompt": "",
        "stream": False,
        "think": False,
        "options": {"num_predict": 1},
        "keep_alive": service.OLLAMA_KEEP_ALIVE,
    }


def test_service_exposes_the_active_mlflow_trace_id(monkeypatch):
    class FakeAgent:
        async def stream_async(self, prompt):
            yield {"data": "hi"}

    fake_mlflow = SimpleNamespace(
        update_current_trace=lambda **kwargs: None,
        get_current_active_span=lambda: SimpleNamespace(trace_id="tr-feedback-1"),
    )
    monkeypatch.setattr(service, "MLFLOW_ENABLED", True)
    monkeypatch.setattr(service, "mlflow", fake_mlflow)
    monkeypatch.setattr(service, "_start_mcp_client", lambda: (None, None))
    monkeypatch.setattr(service, "_build_agent", lambda *args, **kwargs: FakeAgent())

    async def run_inline(function, *args):
        return function(*args)

    # Keep this unit focused on trace emission rather than MCP worker-thread
    # lifecycle; MCP startup behavior is covered by its own tests.
    monkeypatch.setattr(service.asyncio, "to_thread", run_inline)

    async def collect():
        return [chunk async for chunk in service._event_stream("hi", "user-1", "sess-1")]

    events = _decode_sse(asyncio.run(collect()))

    assert events[0] == {"type": "trace", "trace_id": "tr-feedback-1"}
    assert events[1] == {"type": "text", "content": "hi"}


def test_service_creates_and_updates_mlflow_human_feedback(monkeypatch):
    calls = []
    source = SimpleNamespace(source_id="user-1")
    fake_mlflow = SimpleNamespace(
        log_feedback=lambda **kwargs: (
            calls.append(("create", kwargs)) or SimpleNamespace(assessment_id="a-1")
        ),
        get_assessment=lambda **kwargs: SimpleNamespace(
            name=service.MLFLOW_FEEDBACK_NAME,
            source=source,
        ),
        update_assessment=lambda **kwargs: (
            calls.append(("update", kwargs)) or SimpleNamespace(assessment_id="a-1")
        ),
    )
    monkeypatch.setattr(service, "MLFLOW_ENABLED", True)
    monkeypatch.setattr(service, "mlflow", fake_mlflow)

    created = service._save_mlflow_feedback(
        {
            "trace_id": "tr-feedback-1",
            "rating": "up",
            "user_id": "user-1",
            "session_id": "session-1",
            "workspace": "Travel Agent",
        }
    )
    updated = service._save_mlflow_feedback(
        {
            "trace_id": "tr-feedback-1",
            "assessment_id": created["assessment_id"],
            "rating": "down",
            "user_id": "user-1",
        }
    )

    assert created == {
        "trace_id": "tr-feedback-1",
        "assessment_id": "a-1",
        "rating": "up",
    }
    assert updated["rating"] == "down"
    assert calls[0][0] == "create"
    assert calls[0][1]["name"] == "user_satisfaction"
    assert calls[0][1]["value"] is True
    assert calls[0][1]["source"].source_id == "user-1"
    assert calls[1][0] == "update"
    assert calls[1][1]["assessment"].value is False


def test_attachment_manifest_is_added_to_system_prompt(monkeypatch):
    prompt = service._system_prompt_for(
        "Show the result",
        [{"name": "metrics.csv", "path": "/tmp/files/metrics.csv", "size": 42}],
    )

    assert "## Uploaded files" in prompt
    assert "`/tmp/files/metrics.csv`" in prompt
    assert "## Skill: code-interpreter" in prompt


def test_attachment_stream_skips_unrelated_mcp_tools(monkeypatch):
    captured = {}

    class FakeAgent:
        async def stream_async(self, prompt):
            yield {"data": "ready"}

    def fail_if_started():
        raise AssertionError("MCP clients must not start for uploaded-file analysis")

    def fake_build(tool_hook, agent_tools=None, **kwargs):
        captured["agent_tools"] = agent_tools
        return FakeAgent()

    monkeypatch.setattr(service, "_start_mcp_client", fail_if_started)
    monkeypatch.setattr(service, "_build_agent", fake_build)

    async def collect():
        return [
            chunk
            async for chunk in service._event_stream(
                "Analyze the structure",
                attachment_manifest=[
                    {
                        "name": "structure.cif",
                        "path": "/tmp/files/structure.cif",
                        "size": 128,
                        "kind": "mmcif",
                    }
                ],
            )
        ]

    events = _decode_sse(asyncio.run(collect()))

    assert captured["agent_tools"] == []
    assert events == [{"type": "text", "content": "ready"}]


def test_mmcif_manifest_adds_structure_analysis_guidance():
    prompt = service._system_prompt_for(
        "Count atoms and ligands",
        [
            {
                "name": "structure.cif",
                "path": "/tmp/files/structure.cif",
                "size": 128,
                "kind": "mmcif",
            }
        ],
    )

    assert "_atom_site" in prompt
    assert "atom, model, chain, residue" in prompt
    assert "label/auth identifiers" in prompt
    assert "`/tmp/files/structure.cif`" in prompt


def test_ui_builds_attachment_payload_and_extracts_safe_chart():
    ui = _load_ui_module()
    uploaded = SimpleNamespace(
        name="metrics.csv",
        type="text/csv",
        getvalue=lambda: b"month,revenue\nJan,10\n",
    )
    payload = ui._attachment_payload(uploaded)

    assert base64.b64decode(payload[0]["data_base64"]) == uploaded.getvalue()
    result = json.dumps(
        {
            "output": [
                {
                    "stream": "stdout",
                    "data": (
                        '__VEGA_LITE__:{"title":"Revenue","mark":"bar",'
                        '"data":{"values":[{"month":"Jan","revenue":10}]},'
                        '"encoding":{"x":{"field":"month"},"y":{"field":"revenue"}}}'
                    ),
                }
            ]
        }
    )

    specs = ui._chart_specs_from_result(result)
    assert specs[0]["mark"] == "bar"
    assert specs[0]["data"]["values"][0]["revenue"] == 10


def test_ui_builds_mmcif_attachment_payload_for_supported_extensions():
    ui = _load_ui_module()
    content = b"data_demo\nloop_\n_atom_site.group_PDB\n_atom_site.id\nATOM 1\n"

    for extension in ("cif", "mcif", "mmcif"):
        uploaded = SimpleNamespace(
            name=f"structure.{extension}",
            type="chemical/x-cif",
            getvalue=lambda: content,
        )

        payload = ui._attachment_payload(uploaded)

        assert payload[0]["name"] == f"structure.{extension}"
        assert payload[0]["content_type"] == "chemical/x-cif"
        assert base64.b64decode(payload[0]["data_base64"]) == content
        assert ui._is_mmcif_attachment(payload)


def test_mlflow_root_span_usage_is_zeroed_to_avoid_double_count(monkeypatch):
    class FakeSpan:
        def __init__(self):
            self.attributes = {}

        def set_attribute(self, key, value):
            self.attributes[key] = value

    fake_span = FakeSpan()
    fake_mlflow = SimpleNamespace(get_current_active_span=lambda: fake_span)

    monkeypatch.setattr(service, "MLFLOW_ENABLED", True)
    monkeypatch.setattr(service, "mlflow", fake_mlflow)

    service._annotate_mlflow_cost({"inputTokens": 100, "outputTokens": 50, "totalTokens": 150})

    assert fake_span.attributes["mlflow.chat.tokenUsage"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    assert fake_span.attributes["mlflow.llm.cost"] == {
        "input_cost": 0.0,
        "output_cost": 0.0,
        "total_cost": 0.0,
    }


def test_mlflow_agent_version_params_describe_public_config():
    params = service._mlflow_agent_version_params()

    assert params["app"] == "opensource-custom-agent"
    assert params["agent_version"] == service.OPENSOURCE_AGENT_VERSION
    assert params["llm"] == service.MODEL_ID
    assert params["model_provider"] in {"kimi", "ollama", "litellm", "auto"}
    assert params["prompt_template"] == service.SYSTEM_PROMPT
    assert len(params["system_prompt_sha256"]) == 64
    assert len(params["skills_sha256"]) == 64
    assert "API_KEY" not in params
    assert "RAPIDAPI_KEY" not in params


def test_mlflow_agent_version_params_report_mcp_config_separately_from_credentials(
    monkeypatch,
):
    monkeypatch.setattr(service, "BOOKING_MCP_URL", "https://mcp.rapidapi.com")
    monkeypatch.setattr(service, "RAPIDAPI_KEY", "")
    monkeypatch.setattr(service, "AERODATABOX_MCP_URL", "https://mcp.rapidapi.com")
    monkeypatch.setattr(service, "AERODATABOX_RAPIDAPI_KEY", "flight-secret")
    monkeypatch.setattr(service, "AIR_SCRAPER_MCP_URL", "https://mcp.rapidapi.com")
    monkeypatch.setattr(service, "AIR_SCRAPER_RAPIDAPI_KEY", "")
    monkeypatch.setattr(service, "GEOAPIFY_MCP_URL", "https://api.geoapify.com/v1/mcp")
    monkeypatch.setattr(service, "GEOAPIFY_API_KEY", "geo-secret")

    params = service._mlflow_agent_version_params()

    assert params["booking_mcp_enabled"] == "True"
    assert params["booking_mcp_credentials_configured"] == "False"
    assert params["aerodatabox_mcp_enabled"] == "True"
    assert params["aerodatabox_mcp_credentials_configured"] == "True"
    assert params["air_scraper_mcp_enabled"] == "True"
    assert params["air_scraper_mcp_credentials_configured"] == "False"
    assert params["geoapify_mcp_enabled"] == "True"
    assert params["geoapify_mcp_credentials_configured"] == "True"


def test_configure_mlflow_agent_version_sets_active_model_and_logs_params(monkeypatch):
    calls = []

    class FakeActiveModel:
        model_id = "m-test"

    fake_mlflow = SimpleNamespace(
        set_active_model=lambda **kwargs: (
            calls.append(("set_active_model", kwargs)) or FakeActiveModel()
        ),
        log_model_params=lambda params, model_id=None: calls.append(
            ("log_model_params", {"params": params, "model_id": model_id})
        ),
    )
    monkeypatch.setattr(service, "MLFLOW_ENABLED", True)
    monkeypatch.setattr(service, "mlflow", fake_mlflow)
    monkeypatch.setattr(service, "MLFLOW_ACTIVE_MODEL_NAME", "opensource-custom-agent-test")
    monkeypatch.setattr(service, "MLFLOW_LOG_MODEL_PARAMS", True)

    model_id = service._configure_mlflow_agent_version()

    assert model_id == "m-test"
    assert calls[0] == (
        "set_active_model",
        {"name": "opensource-custom-agent-test"},
    )
    assert calls[1][0] == "log_model_params"
    assert calls[1][1]["model_id"] == "m-test"
    assert calls[1][1]["params"]["app"] == "opensource-custom-agent"


def test_tool_hook_records_mlflow_tool_span(monkeypatch):
    class FakeSpan:
        def __init__(self):
            self.end_args = None

        def end(self, **kwargs):
            self.end_args = kwargs

    root_span = object()
    tool_span = FakeSpan()
    calls = []
    fake_mlflow = SimpleNamespace(
        get_current_active_span=lambda: root_span,
        start_span_no_context=lambda **kwargs: calls.append(kwargs) or tool_span,
    )
    monkeypatch.setattr(service, "MLFLOW_ENABLED", True)
    monkeypatch.setattr(service, "mlflow", fake_mlflow)

    hook = service.ToolEventHook(service.queue.Queue())
    tool_use = {
        "toolUseId": "tool-1",
        "name": "searchLocation",
        "input": {"query": "Antibes"},
    }
    hook._on_before(SimpleNamespace(tool_use=tool_use))
    hook._on_after(SimpleNamespace(tool_use=tool_use, result={"content": "Antibes, France"}))

    assert calls == [
        {
            "name": "searchLocation",
            "span_type": "TOOL",
            "parent_span": root_span,
            "inputs": {"query": "Antibes"},
        }
    ]
    assert tool_span.end_args == {"outputs": "Antibes, France", "status": "OK"}


def test_tool_hook_closes_interrupted_mlflow_span_as_error(monkeypatch):
    class FakeSpan:
        def __init__(self):
            self.end_args = None

        def end(self, **kwargs):
            self.end_args = kwargs

    tool_span = FakeSpan()
    fake_mlflow = SimpleNamespace(
        get_current_active_span=lambda: object(),
        start_span_no_context=lambda **kwargs: tool_span,
    )
    monkeypatch.setattr(service, "MLFLOW_ENABLED", True)
    monkeypatch.setattr(service, "mlflow", fake_mlflow)

    hook = service.ToolEventHook(service.queue.Queue())
    hook._on_before(
        SimpleNamespace(tool_use={"toolUseId": "tool-1", "name": "webSearch", "input": {}})
    )
    hook.close_open_spans()

    assert tool_span.end_args == {
        "status": "ERROR",
        "attributes": {"interrupted": True},
    }


def test_litellm_router_is_visible_as_an_mlflow_child_span(monkeypatch):
    class FakeSpan:
        def __init__(self):
            self.end_args = None

        def end(self, **kwargs):
            self.end_args = kwargs

    parent = object()
    router_span = FakeSpan()
    calls = []
    fake_mlflow = SimpleNamespace(
        start_span_no_context=lambda **kwargs: calls.append(kwargs) or router_span,
    )
    monkeypatch.setattr(service, "MLFLOW_ENABLED", True)
    monkeypatch.setattr(service, "mlflow", fake_mlflow)

    service._record_mlflow_router_span(
        {
            "router_model_name": "opensource-auto-router",
            "routing_decision": {
                "router_type": "complexity",
                "routed_model": "opensource-ollama",
                "tier": "SIMPLE",
                "cause": "heuristic_scorer",
                "score": 0.12,
                "signals": ["prompt keyword must not be retained"],
            },
            "status": "OK",
            "latency_ms": 0.8,
            "start_time_ns": 100,
            "end_time_ns": 200,
        },
        parent,
    )

    assert calls[0]["name"] == "litellm_router"
    assert calls[0]["span_type"] == "CHAIN"
    assert calls[0]["parent_span"] is parent
    assert calls[0]["attributes"]["router.decision.tier"] == "SIMPLE"
    assert "signals" not in router_span.end_args["outputs"]
    assert router_span.end_args["outputs"]["routed_model"] == "opensource-ollama"
    assert router_span.end_args["status"] == "OK"
    assert router_span.end_args["end_time_ns"] == 200


def test_ui_session_id_stable_within_chat_and_rotates_on_new_chat():
    ui = _load_ui_module()
    ui.st.session_state[ui.WORKSPACE_KEY] = ui.TRAVEL_WORKSPACE
    first = ui._current_session_id()
    assert ui._current_session_id() == first
    assert ui._new_state(first)["session_id"] == first
    ui.start_new_chat()
    assert ui._current_session_id() != first


def test_ui_workspaces_isolate_sessions_prompts_and_agent_labels():
    ui = _load_ui_module()

    ui.st.session_state[ui.WORKSPACE_KEY] = ui.TRAVEL_WORKSPACE
    travel_session = ui._current_session_id()
    travel_prompt_key = ui.current_prompt_key()
    travel_history = ui._history()
    travel_history.append({"id": "travel-run"})
    assert ui._new_state()["arch_name"] == "Travel Planner Agent"

    ui.st.session_state[ui.WORKSPACE_KEY] = ui.PROTEIN_WORKSPACE
    protein_session = ui._current_session_id()
    protein_prompt_key = ui.current_prompt_key()
    protein_history = ui._history()
    assert ui._new_state()["arch_name"] == "Protein Analysis Agent"

    assert protein_session != travel_session
    assert protein_prompt_key != travel_prompt_key
    assert protein_history == []
    assert protein_history is not travel_history

    ui.start_new_chat()
    assert ui._current_session_id() != protein_session
    ui.st.session_state[ui.WORKSPACE_KEY] = ui.TRAVEL_WORKSPACE
    assert ui._current_session_id() == travel_session
    assert ui._history() == [{"id": "travel-run"}]


def test_ui_workspaces_have_separate_upload_types_and_prompts():
    ui = _load_ui_module()

    travel = ui.WORKSPACES[ui.TRAVEL_WORKSPACE]
    protein = ui.WORKSPACES[ui.PROTEIN_WORKSPACE]

    assert travel["upload_types"] == ("csv",)
    assert protein["upload_types"] == ("cif", "mcif", "mmcif")
    assert travel["primary_prompts"] != protein["primary_prompts"]


def test_ui_catalog_searches_names_descriptions_and_capabilities():
    ui = _load_ui_module()

    assert ui._matching_workspaces("") == [ui.TRAVEL_WORKSPACE, ui.PROTEIN_WORKSPACE]
    assert ui._matching_workspaces("weather") == [ui.TRAVEL_WORKSPACE]
    assert ui._matching_workspaces("mmcif") == [ui.PROTEIN_WORKSPACE]
    assert ui._matching_workspaces("notebook") == [ui.PROTEIN_WORKSPACE]
    assert ui._matching_workspaces("not-a-real-capability") == []


def test_ui_reducer_keeps_live_tool_trace_and_answer():
    ui = _load_ui_module()
    state = ui._new_state()

    ui._apply_agent_event(state, {"type": "text", "content": "I will check. "})
    ui._apply_agent_event(
        state,
        {
            "type": "tool_start",
            "tool_use_id": "tool-1",
            "name": "searchLocation",
            "input": {"query": "Antibes"},
        },
    )
    ui._apply_agent_event(
        state,
        {
            "type": "tool_start",
            "tool_use_id": "tool-1",
            "name": "searchLocation",
            "input": {"query": "Antibes, France"},
        },
    )
    ui._apply_agent_event(
        state,
        {
            "type": "tool_end",
            "tool_use_id": "tool-1",
            "name": "searchLocation",
            "result": "Antibes, France",
        },
    )
    ui._apply_agent_event(state, {"type": "text", "content": "It is 22C."})
    ui._apply_agent_event(state, {"type": "metrics", "tool_calls": 0, "usage": {}})

    assert state["answer"] == "It is 22C."
    assert state["trace"].startswith("I will check.")
    assert "Antibes, France" in state["trace"]
    assert len(state["tools"]) == 1
    assert state["tools"][0]["input"] == {"query": "Antibes, France"}
    assert state["metrics"]["tool_calls"] == 1


def test_ui_reducer_keeps_mlflow_trace_id_for_feedback():
    ui = _load_ui_module()
    state = ui._new_state()

    ui._apply_agent_event(state, {"type": "trace", "trace_id": "tr-feedback-1"})

    assert state["mlflow_trace_id"] == "tr-feedback-1"
    assert ui._snapshot_state(state)["mlflow_trace_id"] == "tr-feedback-1"


def test_ui_keeps_selected_model_with_response():
    ui = _load_ui_module()
    state = ui._new_state()

    ui._apply_agent_event(
        state,
        {
            "type": "metrics",
            "model_id": "kimi-k2.6",
            "model_router": "litellm-auto",
            "usage": {},
        },
    )

    assert ui._response_model(state) == "kimi-k2.6"
    assert ui._response_model(ui._snapshot_state(state)) == "kimi-k2.6"
    assert "kimi-k2.6" in ui._model_badge(state)


def test_ui_shows_request_to_model_dispatch_timing():
    ui = _load_ui_module()
    state = ui._new_state()
    ui._apply_agent_event(
        state,
        {
            "type": "metrics",
            "request_to_model_dispatch_ms": 18.42,
            "usage": {},
        },
    )

    chips = ui._header_chips(state)
    assert "Model dispatch" in chips
    assert "18.42ms" in chips


def test_ui_submits_feedback_to_agent_service(monkeypatch):
    ui = _load_ui_module()
    ui.st.session_state[ui.WORKSPACE_KEY] = ui.TRAVEL_WORKSPACE
    monkeypatch.setattr(ui, "AGENT_URL", "http://agent:8080")
    monkeypatch.setattr(ui, "_current_user_id", lambda: "user-1")
    requests = []

    class Response:
        is_error = False

        @staticmethod
        def json():
            return {"assessment_id": "a-1", "rating": "up", "trace_id": "tr-1"}

    def fake_post(url, **kwargs):
        requests.append((url, kwargs))
        return Response()

    monkeypatch.setattr(ui.httpx, "post", fake_post)
    state = ui._new_state("session-1")
    state["mlflow_trace_id"] = "tr-1"

    result = ui._submit_mlflow_feedback(state, "up")

    assert result["assessment_id"] == "a-1"
    assert requests[0][0] == "http://agent:8080/feedback"
    assert requests[0][1]["json"] == {
        "trace_id": "tr-1",
        "assessment_id": None,
        "rating": "up",
        "user_id": "user-1",
        "session_id": "session-1",
        "workspace": "Travel Agent",
    }
