"""M0.2 integration: output filter is wired into the real stream runtime factory."""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest
import yaml

import orchestrator.stream_runtime as stream_runtime_module
from interfaces.base import HealthStatus
from interfaces.input import InputEvent, InputService
from interfaces.llm import LLMRequest, LLMToken
from orchestrator.config_loader import ConfigLoader
from orchestrator.features import FeatureStatus
from orchestrator.stream_runtime import StreamRuntime, StreamRuntimeConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
BAD_OUTPUT = "Tớ chỉ là một chương trình thôi, tớ không có cảm xúc."
CLEAN_OUTPUT = "Ừ, hỏi gì thì hỏi đi."


class ScriptedStreamLLM:
    service_id = "llm_main"

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.requests: list[LLMRequest] = []
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def health_check(self) -> HealthStatus:
        return HealthStatus.healthy(self.service_id)

    async def generate_stream(self, request: LLMRequest):
        self.requests.append(request)
        if not self.outputs:
            raise AssertionError("Fake LLM ran out of scripted outputs")
        text = self.outputs.pop(0)
        yield LLMToken(request_id=request.request_id, token=text, is_final=False)
        yield LLMToken(request_id=request.request_id, token="", is_final=True)

    async def cancel(self, request_id: str) -> None:
        return None

    def get_metrics(self) -> dict[str, Any]:
        return {}


class CaptureWriter:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, record: dict[str, Any]) -> None:
        self.records.append(record)


class FakeInputService(InputService):
    service_id = "fake_input"

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def health_check(self) -> HealthStatus:
        return HealthStatus.healthy(self.service_id)

    def get_metrics(self) -> dict[str, Any]:
        return {}

    async def event_stream(self) -> AsyncIterator[InputEvent]:
        if False:
            yield  # pragma: no cover


def _loader(tmp_path: Path, *, filter_enabled: bool) -> ConfigLoader:
    config_dir = tmp_path / "config"
    shutil.copytree(REPO_ROOT / "config", config_dir)
    features_path = config_dir / "features.yaml"
    features = yaml.safe_load(features_path.read_text(encoding="utf-8"))
    features["features"]["filter_rule"]["enabled"] = filter_enabled
    features_path.write_text(
        yaml.safe_dump(features, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    operations_path = config_dir / "operations.yaml"
    operations = yaml.safe_load(operations_path.read_text(encoding="utf-8"))
    operations["shutdown"]["snapshot_file"] = str(tmp_path / "last_runtime_snapshot.json")
    operations["dashboard_standalone"]["operator_audit_file"] = str(
        tmp_path / "operator_audit.jsonl"
    )
    operations["incident_log"]["file"] = str(tmp_path / "incidents.jsonl")
    operations_path.write_text(
        yaml.safe_dump(operations, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    loader = ConfigLoader(config_dir)
    loader.load_all()
    return loader


async def _build_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    outputs: list[str],
    *,
    filter_enabled: bool,
    dashboard_capture: dict[str, Any] | None = None,
) -> tuple[StreamRuntime, ScriptedStreamLLM, CaptureWriter]:
    fake_llm = ScriptedStreamLLM(outputs)
    pref_writer = CaptureWriter()
    monkeypatch.setattr(
        stream_runtime_module,
        "LlamaCppLLMService",
        SimpleNamespace(from_loader=lambda _loader: fake_llm),
    )
    monkeypatch.setattr(stream_runtime_module, "setup_from_config", lambda _loader: None)
    monkeypatch.setattr(
        stream_runtime_module,
        "_make_pref_logger",
        lambda _loader: pref_writer,
    )

    class FakeProcessManager:
        def __init__(self, _config) -> None:
            self.started = False

        async def start(self) -> None:
            self.started = True

        async def stop(self) -> None:
            self.started = False

        async def restart(self) -> None:
            self.started = True

    monkeypatch.setattr(stream_runtime_module, "LlamaServerProcessManager", FakeProcessManager)

    enable_dashboard = dashboard_capture is not None
    if dashboard_capture is not None:
        import dashboard.dashboard_server as dashboard_module

        class FakeDashboard:
            def __init__(self, **kwargs: Any) -> None:
                dashboard_capture.update(kwargs)

            async def serve(self) -> None:
                await asyncio.sleep(0)

        monkeypatch.setattr(dashboard_module, "DashboardServer", FakeDashboard)

    runtime = await stream_runtime_module.build_stream_runtime(
        loader=_loader(tmp_path, filter_enabled=filter_enabled),
        sources=[FakeInputService()],
        cfg=StreamRuntimeConfig(
            enable_tts=False,
            enable_memory=False,
            enable_autonomy=False,
            enable_dashboard=enable_dashboard,
        ),
    )
    return runtime, fake_llm, pref_writer


class TestRuntimeFilterWiring:
    async def test_enabled_filter_regenerates_and_runtime_toggle_is_real(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        dashboard: dict[str, Any] = {}
        runtime, llm, pref = await _build_runtime(
            monkeypatch,
            tmp_path,
            [BAD_OUTPUT, CLEAN_OUTPUT, BAD_OUTPUT, BAD_OUTPUT, CLEAN_OUTPUT],
            filter_enabled=True,
            dashboard_capture=dashboard,
        )
        try:
            parsed, level = await runtime._runner.run_turn("r1", "chào")
            assert level == 0
            assert parsed.text == CLEAN_OUTPUT
            assert len(llm.requests) == 2
            assert runtime._runner.last_filter_verdict is not None
            assert runtime._runner.last_filter_verdict.passed is True

            metrics = runtime._metrics.filter_snapshot()
            assert metrics["checks_total"] == 1
            assert metrics["hits_total"] == 1
            assert metrics["by_category"] == {"persona_break": 1}
            assert runtime._regenerator.get_metrics()["filter_regen_recovered_total"] == 1
            prometheus = runtime._metrics.prometheus_text().decode("utf-8")
            assert 'mai_filter_regen_total{result="recovered"} 1.0' in prometheus
            assert len(pref.records) == 1
            assert pref.records[0]["rejected"] == BAD_OUTPUT
            assert pref.records[0]["chosen"] == CLEAN_OUTPUT
            assert pref.records[0]["reason"] == "filter:persona_break"

            assert dashboard["feature_manager"] is runtime._feature_manager
            assert dashboard["filter_svc"] is runtime._filter_svc
            assert dashboard["regenerator"] is runtime._regenerator
            assert dashboard["goal_manager"] is runtime.goal_manager
            assert set(runtime._health_supervisor.snapshot()["targets"]) == {
                "dashboard", "input_router", "llm_main",
            }
            assert runtime._llama_process_manager.started is True
            assert dashboard["control_plane"] is runtime._control_plane
            assert runtime.agent_state.snapshot().active_goal_ref is None
            assert runtime.goal_proposal.enabled is False

            proposal_toggle = await runtime._feature_manager.enable("goal_proposals", user="test")
            assert proposal_toggle.ok is True
            assert runtime.goal_proposal.enabled is True
            proposal_toggle = await runtime._feature_manager.disable("goal_proposals", user="test")
            assert proposal_toggle.ok is True
            assert runtime.goal_proposal.enabled is False

            result = await runtime._feature_manager.disable("filter_rule", user="test")
            assert result.ok is True
            assert result.status is FeatureStatus.DISABLED
            assert runtime._runner.filter_enabled is False
            unfiltered, _ = await runtime._runner.run_turn("r2", "chào lại")
            assert unfiltered.text == BAD_OUTPUT
            assert runtime._runner.last_filter_verdict is None
            assert runtime._metrics.filter_snapshot()["checks_total"] == 1

            result = await runtime._feature_manager.enable("filter_rule", user="test")
            assert result.ok is True
            assert result.status is FeatureStatus.ENABLED
            assert runtime._runner.filter_enabled is True
            filtered_again, _ = await runtime._runner.run_turn("r3", "lần nữa")
            assert filtered_again.text == CLEAN_OUTPUT
            assert runtime._metrics.filter_snapshot()["checks_total"] == 2
            assert len(pref.records) == 2
        finally:
            await runtime.stop()
        snapshot_path = tmp_path / "last_runtime_snapshot.json"
        assert snapshot_path.exists()
        shutdown_snapshot = yaml.safe_load(snapshot_path.read_text(encoding="utf-8"))
        assert shutdown_snapshot["agent"] is not None
        assert runtime._shutdown_coordinator.get_metrics()["shutdown_completed"] is True

    async def test_disabled_feature_is_backward_compatible(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        runtime, llm, pref = await _build_runtime(
            monkeypatch,
            tmp_path,
            [BAD_OUTPUT],
            filter_enabled=False,
        )
        try:
            parsed, level = await runtime._runner.run_turn("r1", "chào")
            assert level == 0
            assert parsed.text == BAD_OUTPUT
            assert len(llm.requests) == 1
            assert runtime._runner.filter_enabled is False
            assert runtime._runner.last_filter_verdict is None
            assert runtime._metrics.filter_snapshot()["checks_total"] == 0
            assert pref.records == []
        finally:
            await runtime.stop()

    async def test_filter_exception_fails_open_and_is_observable(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from services.filter.rule_filter import RuleFilter

        class ExplodingFilter:
            async def start(self) -> None:
                return None

            async def stop(self) -> None:
                return None

            async def health_check(self) -> HealthStatus:
                return HealthStatus.healthy("filter")

            async def check(self, text: str, context: dict[str, Any] | None = None):
                raise RuntimeError("filter exploded")

            def get_metrics(self) -> dict[str, Any]:
                return {"filter_fail_open_total": 1}

        monkeypatch.setattr(
            RuleFilter,
            "from_config",
            classmethod(lambda cls, loader, event_bus=None: ExplodingFilter()),
        )
        runtime, _llm, pref = await _build_runtime(
            monkeypatch,
            tmp_path,
            [BAD_OUTPUT],
            filter_enabled=True,
        )
        try:
            parsed, level = await runtime._runner.run_turn("r1", "chào")
            assert level == 0
            assert parsed.text == BAD_OUTPUT
            assert runtime._runner.last_filter_verdict is not None
            assert runtime._runner.last_filter_verdict.passed is True
            assert runtime._runner.last_filter_verdict.reason.startswith("fail-open")
            metrics = runtime._metrics.filter_snapshot()
            assert metrics["checks_total"] == 1
            assert metrics["fail_open_total"] == 1
            assert pref.records == []
        finally:
            await runtime.stop()
