"""Test FeatureManager: toggle rules 1-6 (ARCHITECTURE 4.4).

Phase 0 DoD: "Dashboard mở ở localhost, toggle giả bật/tắt được" — 0.B lo
phần logic toggle, 0.F lo phần UI.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from orchestrator.config_loader import ConfigError, ConfigLoader
from orchestrator.features import (
    CoreFeatureError,
    Feature,
    FeatureManager,
    FeatureStatus,
    UnknownFeatureError,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def write_feature_config(
    root: Path,
    features: dict[str, object],
    *,
    total_mb: object = 1000,
) -> ConfigLoader:
    (root / "system.yaml").write_text(
        yaml.safe_dump({
            "resources": {
                "vram_total_mb": total_mb,
                "vram_reserved_mb": 0,
                "vram_buffer_mb": 0,
            },
            "features": {"core": ["llm_main"]},
        }, sort_keys=False),
        encoding="utf-8",
    )
    (root / "features.yaml").write_text(
        yaml.safe_dump({"features": features}, sort_keys=False),
        encoding="utf-8",
    )
    loader = ConfigLoader(root)
    loader.load_all()
    return loader


def make_feature(fid: str, **kw) -> Feature:
    defaults = dict(
        name=fid,
        description="",
        category="test",
        default_enabled=False,
    )
    defaults.update(kw)
    return Feature(id=fid, **defaults)  # type: ignore[arg-type]


@pytest.fixture
def mgr() -> FeatureManager:
    m = FeatureManager(vram_budget_mb=1000, core_feature_ids=("llm_main", "state_machine"))
    m.register(make_feature("a", vram_cost_mb=100))
    m.register(make_feature("b", vram_cost_mb=200, depends_on=["a"]))
    m.register(make_feature("heavy", vram_cost_mb=5000))
    m.register(make_feature("saver", vram_cost_mb=-500))
    m.register(make_feature("x", conflicts_with=["y"]))
    m.register(make_feature("y", conflicts_with=["x"]))
    return m


class TestRegistryBasics:
    async def test_unknown_feature_raises(self, mgr: FeatureManager) -> None:
        with pytest.raises(UnknownFeatureError):
            await mgr.get_status("nope")

    async def test_list_features_sorted(self, mgr: FeatureManager) -> None:
        ids = [f.id for f in await mgr.list_features()]
        assert ids == sorted(ids)

    async def test_default_status_disabled(self, mgr: FeatureManager) -> None:
        assert await mgr.get_status("a") is FeatureStatus.DISABLED

    def test_duplicate_registration_is_rejected(self, mgr: FeatureManager) -> None:
        with pytest.raises(ValueError, match="feature id trùng"):
            mgr.register(make_feature("a"))

    async def test_production_registers_director_chat_gate(self) -> None:
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        production = FeatureManager.from_config(loader)

        assert await production.get_status("director_chat_gate") is FeatureStatus.ENABLED


class TestCoreFeatures:
    async def test_enable_core_raises(self, mgr: FeatureManager) -> None:
        mgr.register(make_feature("llm_main"))
        with pytest.raises(CoreFeatureError, match="core feature"):
            await mgr.enable("llm_main")

    async def test_disable_core_raises(self, mgr: FeatureManager) -> None:
        mgr.register(make_feature("state_machine", default_enabled=True,
                                 current_status=FeatureStatus.ENABLED))
        with pytest.raises(CoreFeatureError):
            await mgr.disable("state_machine")

    def test_is_core(self, mgr: FeatureManager) -> None:
        assert mgr.is_core("llm_main") is True
        assert mgr.is_core("a") is False


class TestEnableDisable:
    async def test_enable_then_disable(self, mgr: FeatureManager) -> None:
        r = await mgr.enable("a")
        assert r.ok is True
        assert await mgr.get_status("a") is FeatureStatus.ENABLED

        r = await mgr.disable("a")
        assert r.ok is True
        assert await mgr.get_status("a") is FeatureStatus.DISABLED

    async def test_enable_already_enabled_is_idempotent(self, mgr: FeatureManager) -> None:
        await mgr.enable("a")
        r = await mgr.enable("a")
        assert r.ok is True

    async def test_disable_already_disabled_is_idempotent(self, mgr: FeatureManager) -> None:
        r = await mgr.disable("a")
        assert r.ok is True

    async def test_enabled_ids_tracks_state(self, mgr: FeatureManager) -> None:
        assert mgr.enabled_ids() == []
        await mgr.enable("a")
        await mgr.enable("x")
        assert mgr.enabled_ids() == ["a", "x"]


class TestDependencyCheck:
    """Rule 3: bật X cần depends_on của X đã bật."""

    async def test_enable_blocked_when_dependency_off(self, mgr: FeatureManager) -> None:
        r = await mgr.enable("b")
        assert r.ok is False
        assert "thiếu dependency" in r.reason
        assert "a" in r.reason
        assert await mgr.get_status("b") is FeatureStatus.DISABLED

    async def test_enable_succeeds_after_dependency_on(self, mgr: FeatureManager) -> None:
        await mgr.enable("a")
        r = await mgr.enable("b")
        assert r.ok is True

    async def test_core_dependency_is_always_satisfied(self, mgr: FeatureManager) -> None:
        mgr.register(make_feature("core_child", depends_on=["llm_main"]))
        assert (await mgr.enable("core_child")).ok is True

    async def test_disable_blocked_when_dependent_still_on(self, mgr: FeatureManager) -> None:
        await mgr.enable("a")
        await mgr.enable("b")
        r = await mgr.disable("a")
        assert r.ok is False
        assert "đang được cần bởi" in r.reason
        assert await mgr.get_status("a") is FeatureStatus.ENABLED

    async def test_disable_succeeds_after_dependent_off(self, mgr: FeatureManager) -> None:
        await mgr.enable("a")
        await mgr.enable("b")
        await mgr.disable("b")
        assert (await mgr.disable("a")).ok is True

    async def test_dependency_graph_transitive(self, mgr: FeatureManager) -> None:
        mgr.register(make_feature("c", depends_on=["b"]))
        g = await mgr.get_dependencies("c")
        assert g.requires == ["a", "b"]  # transitive qua b
        g_a = await mgr.get_dependencies("a")
        assert g_a.required_by == ["b", "c"]

    async def test_dependency_graph_reports_conflicts(self, mgr: FeatureManager) -> None:
        g = await mgr.get_dependencies("x")
        assert g.conflicts == ["y"]


class TestConflictCheck:
    """Rule 4: không bật X nếu conflicts_with đang bật."""

    async def test_enable_blocked_by_active_conflict(self, mgr: FeatureManager) -> None:
        await mgr.enable("x")
        r = await mgr.enable("y")
        assert r.ok is False
        assert "xung đột với" in r.reason
        assert await mgr.get_status("y") is FeatureStatus.DISABLED

    async def test_enable_ok_after_conflict_disabled(self, mgr: FeatureManager) -> None:
        await mgr.enable("x")
        await mgr.disable("x")
        assert (await mgr.enable("y")).ok is True

    async def test_reverse_declared_conflict_is_enforced(self) -> None:
        manager = FeatureManager(vram_budget_mb=1000)
        manager.register(make_feature("a"))
        manager.register(make_feature("b", conflicts_with=["a"]))
        await manager.enable("b")
        result = await manager.enable("a")
        assert result.ok is False
        assert "b" in result.reason


class TestResourceCheck:
    """Rule 5: VRAM budget."""

    async def test_enable_blocked_when_over_budget(self, mgr: FeatureManager) -> None:
        r = await mgr.enable("heavy")  # 5000MB > budget 1000MB
        assert r.ok is False
        assert "thiếu VRAM" in r.reason

    async def test_negative_cost_always_allowed(self, mgr: FeatureManager) -> None:
        """kv_cache_q8 có cost âm (tiết kiệm VRAM) — không bao giờ bị chặn."""
        assert (await mgr.enable("saver")).ok is True

    async def test_used_vram_sums_enabled_only(self, mgr: FeatureManager) -> None:
        assert mgr.used_vram_mb() == 0
        await mgr.enable("a")
        assert mgr.used_vram_mb() == 100
        await mgr.enable("saver")
        assert mgr.used_vram_mb() == -400  # 100 + (-500)

    async def test_check_resources_reports_available(self, mgr: FeatureManager) -> None:
        await mgr.enable("a")  # dùng 100 / 1000
        chk = await mgr.check_resources("b")
        assert chk.ok is True
        assert chk.requested_mb == 200
        assert chk.available_mb == 900

    async def test_check_resources_for_enabled_feature_requests_zero(self, mgr: FeatureManager) -> None:
        await mgr.enable("a")
        chk = await mgr.check_resources("a")
        assert chk.ok is True
        assert chk.requested_mb == 0

    async def test_budget_freed_after_disable(self, mgr: FeatureManager) -> None:
        m = FeatureManager(vram_budget_mb=250)
        m.register(make_feature("p", vram_cost_mb=200))
        m.register(make_feature("q", vram_cost_mb=200))
        assert (await m.enable("p")).ok is True
        assert (await m.enable("q")).ok is False  # chỉ còn 50MB
        await m.disable("p")
        assert (await m.enable("q")).ok is True


class TestHandlersAndRollback:
    """Rule 1 + 6: atomic + rollback khi handler fail."""

    async def test_enable_handler_called(self, mgr: FeatureManager) -> None:
        calls: list[str] = []

        async def on_enable():
            calls.append("enabled")

        mgr.attach_handlers("a", enable=on_enable)
        await mgr.enable("a")
        assert calls == ["enabled"]

    async def test_disable_handler_called(self, mgr: FeatureManager) -> None:
        calls: list[str] = []

        async def on_disable():
            calls.append("disabled")

        mgr.attach_handlers("a", disable=on_disable)
        await mgr.enable("a")
        await mgr.disable("a")
        assert calls == ["disabled"]

    async def test_rollback_when_enable_handler_raises(self, mgr: FeatureManager) -> None:
        async def boom():
            raise RuntimeError("model load failed")

        mgr.attach_handlers("a", enable=boom)
        r = await mgr.enable("a")
        assert r.ok is False
        assert "handler lỗi" in r.reason
        assert "model load failed" in r.reason
        assert await mgr.get_status("a") is FeatureStatus.ERROR

    async def test_rollback_when_disable_handler_raises(self, mgr: FeatureManager) -> None:
        async def boom():
            raise RuntimeError("cleanup failed")

        mgr.attach_handlers("a", disable=boom)
        await mgr.enable("a")
        r = await mgr.disable("a")
        assert r.ok is False
        assert await mgr.get_status("a") is FeatureStatus.ERROR

    async def test_failed_enable_does_not_consume_vram(self, mgr: FeatureManager) -> None:
        async def boom():
            raise RuntimeError("nope")

        mgr.attach_handlers("a", enable=boom)
        await mgr.enable("a")
        # status ERROR (không phải ENABLED) → không tính vào used_vram
        assert mgr.used_vram_mb() == 0


class TestHealthRefresh:
    async def test_failing_health_marks_degraded(self, mgr: FeatureManager) -> None:
        async def unhealthy():
            return False

        mgr.attach_handlers("a", health=unhealthy)
        await mgr.enable("a")
        statuses = await mgr.refresh_health()
        assert statuses["a"] is FeatureStatus.DEGRADED

    async def test_health_exception_marks_degraded(self, mgr: FeatureManager) -> None:
        async def boom():
            raise RuntimeError("probe failed")

        mgr.attach_handlers("a", health=boom)
        await mgr.enable("a")
        statuses = await mgr.refresh_health()
        assert statuses["a"] is FeatureStatus.DEGRADED

    async def test_passing_health_stays_enabled(self, mgr: FeatureManager) -> None:
        async def ok():
            return True

        mgr.attach_handlers("a", health=ok)
        await mgr.enable("a")
        statuses = await mgr.refresh_health()
        assert statuses["a"] is FeatureStatus.ENABLED

    async def test_degraded_counts_as_enabled(self, mgr: FeatureManager) -> None:
        """DEGRADED vẫn đang chạy → vẫn chiếm VRAM, vẫn thoả dependency."""
        async def unhealthy():
            return False

        mgr.attach_handlers("a", health=unhealthy)
        await mgr.enable("a")
        await mgr.refresh_health()
        assert mgr.used_vram_mb() == 100
        assert (await mgr.enable("b")).ok is True


class TestPersistence:
    """Rule: state persist ở config/features.yaml (4.5)."""

    async def test_persist_writes_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "features.yaml"
        m = FeatureManager(vram_budget_mb=1000, persist_path=path)
        m.register(make_feature(
            "a", vram_cost_mb=100, name="Feature A", description="kept metadata",
        ))
        await m.enable("a")

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["features"]["a"]["enabled"] is True
        assert data["features"]["a"]["vram_cost_mb"] == 100
        assert data["features"]["a"]["name"] == "Feature A"
        assert data["features"]["a"]["description"] == "kept metadata"

    async def test_persist_reflects_disable(self, tmp_path: Path) -> None:
        path = tmp_path / "features.yaml"
        m = FeatureManager(vram_budget_mb=1000, persist_path=path)
        m.register(make_feature("a"))
        await m.enable("a")
        await m.disable("a")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["features"]["a"]["enabled"] is False

    async def test_no_persist_path_is_noop(self, mgr: FeatureManager) -> None:
        assert (await mgr.enable("a")).ok is True  # không raise dù persist_path=None

    async def test_rejected_toggle_does_not_persist(self, tmp_path: Path) -> None:
        path = tmp_path / "features.yaml"
        m = FeatureManager(vram_budget_mb=100, persist_path=path)
        m.register(make_feature("heavy", vram_cost_mb=9999))
        r = await m.enable("heavy")
        assert r.ok is False
        assert not path.exists()

    async def test_existing_yaml_preserves_comment_metadata_and_custom_key(
        self, tmp_path: Path,
    ) -> None:
        path = tmp_path / "features.yaml"
        original = (
            "features:\n"
            "  demo:\n"
            "    enabled: false  # operator state\n"
            "    vram_cost_mb: 0\n"
            "    latency_impact_ms: 0\n"
            "    category: test\n"
            "    depends_on: []\n"
            "    conflicts_with: []\n"
            "    name: Demo feature\n"
            "    description: Keep me\n"
            "    custom_key: custom-value\n"
        )
        path.write_bytes(original.replace("\n", "\r\n").encode("utf-8"))
        manager = FeatureManager(vram_budget_mb=1000, persist_path=path)
        manager.register(make_feature(
            "demo", name="Demo feature", description="Keep me",
        ))

        assert (await manager.enable("demo")).ok is True
        rendered = path.read_text(encoding="utf-8")
        assert "enabled: true  # operator state" in rendered
        assert "name: Demo feature" in rendered
        assert "description: Keep me" in rendered
        assert "custom_key: custom-value" in rendered
        raw = path.read_bytes()
        assert b"\n" not in raw.replace(b"\r\n", b"")

    async def test_persist_failure_rolls_back_status_and_handler(
        self, tmp_path: Path,
    ) -> None:
        path = tmp_path / "features.yaml"
        path.mkdir()
        manager = FeatureManager(vram_budget_mb=1000, persist_path=path)
        manager.register(make_feature("demo"))
        calls: list[str] = []

        async def enable() -> None:
            calls.append("enable")

        async def disable() -> None:
            calls.append("disable")

        manager.attach_handlers("demo", enable=enable, disable=disable)
        result = await manager.enable("demo")

        assert result.ok is False
        assert "persistence lỗi" in result.reason
        assert await manager.get_status("demo") is FeatureStatus.DISABLED
        assert calls == ["enable", "disable"]


class TestFromConfig:
    """Build từ config thật của repo."""

    def test_loads_real_features_yaml(self) -> None:
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        m = FeatureManager.from_config(loader)
        ids = {f.id for f in m._features.values()}
        assert "filter_rule" in ids
        assert "ambient_talk" in ids
        assert "input_voice" in ids

    async def test_avatar_adapter_requires_strict_embodiment_policy(self) -> None:
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        manager = FeatureManager.from_config(loader)
        features = {item.id: item for item in await manager.list_features()}
        assert features["avatar_action_adapter"].depends_on == [
            "animation_smooth", "embodiment_policy",
        ]
        assert await manager.get_status("avatar_action_adapter") is FeatureStatus.DISABLED
        assert await manager.get_status("embodiment_policy") is FeatureStatus.DISABLED

    def test_core_ids_from_system_yaml(self) -> None:
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        m = FeatureManager.from_config(loader)
        assert m.is_core("llm_main") is True
        assert m.is_core("trigger_manager") is True
        assert m.is_core("filter_rule") is False

    def test_vram_budget_computed(self) -> None:
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        m = FeatureManager.from_config(loader)
        # VieNeu-TTS (400MB) thay viXTTS (1790MB) từ 2026-08 → reserved giảm 9790→8400
        expected = 16384 - 8400 - 1000
        assert m._vram_budget_mb == expected

    async def test_default_enabled_reflects_yaml(self) -> None:
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        m = FeatureManager.from_config(loader)
        # config thật: filter_rule ON, input_voice OFF (STT deferred)
        assert await m.get_status("filter_rule") is FeatureStatus.ENABLED
        assert await m.get_status("input_voice") is FeatureStatus.DISABLED

    async def test_real_config_dependencies_consistent(self) -> None:
        """Mọi depends_on/conflicts_with trong config phải trỏ tới feature tồn tại."""
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        m = FeatureManager.from_config(loader)
        known = {f.id for f in await m.list_features()} | set(
            loader.get("system", "features.core", []) or []
        )
        for f in await m.list_features():
            for dep in f.depends_on:
                assert dep in known, f"{f.id}.depends_on trỏ tới feature không tồn tại: {dep}"
            for c in f.conflicts_with:
                assert c in known, f"{f.id}.conflicts_with trỏ tới feature không tồn tại: {c}"

    async def test_real_config_enabled_set_fits_budget(self) -> None:
        """Feature bật sẵn trong config không được vượt VRAM budget."""
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        m = FeatureManager.from_config(loader)
        assert m.used_vram_mb() <= m._vram_budget_mb, (
            f"config bật sẵn {m.used_vram_mb()}MB > budget {m._vram_budget_mb}MB"
        )

    async def test_real_config_enabled_deps_satisfied(self) -> None:
        """Feature bật sẵn phải có dependency cũng bật sẵn."""
        loader = ConfigLoader(REPO_ROOT / "config")
        loader.load_all()
        m = FeatureManager.from_config(loader)
        enabled = set(m.enabled_ids())
        for fid in enabled:
            f = m._features[fid]
            for dep in f.depends_on:
                assert dep in enabled, f"{fid} bật nhưng dependency {dep} tắt"

    async def test_persisted_toggle_survives_restart_without_rewriting_yaml(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / "system.yaml").write_text(
            "resources:\n"
            "  vram_total_mb: 1000\n"
            "  vram_reserved_mb: 0\n"
            "  vram_buffer_mb: 0\n"
            "features:\n"
            "  core: [llm_main]\n",
            encoding="utf-8",
        )
        feature_path = tmp_path / "features.yaml"
        feature_path.write_text(
            "features:\n"
            "  demo:\n"
            "    enabled: false  # survives restart\n"
            "    vram_cost_mb: 0\n"
            "    latency_impact_ms: 0\n"
            "    category: test\n"
            "    depends_on: []\n"
            "    conflicts_with: []\n"
            "    name: Demo feature\n"
            "    description: Restart contract\n",
            encoding="utf-8",
        )
        loader = ConfigLoader(tmp_path)
        loader.load_all()
        manager = FeatureManager.from_config(loader, persist=True)

        assert manager._persist_path == feature_path
        assert (await manager.enable("demo", user="dashboard")).ok is True
        assert "enabled: true  # survives restart" in feature_path.read_text(encoding="utf-8")

        restarted_loader = ConfigLoader(tmp_path)
        restarted_loader.load_all()
        restarted = FeatureManager.from_config(restarted_loader)
        assert await restarted.get_status("demo") is FeatureStatus.ENABLED

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("enabled", "false"),
            ("vram_cost_mb", "0"),
            ("latency_impact_ms", True),
            ("depends_on", "llm_main"),
            ("conflicts_with", [1]),
            ("category", 1),
        ],
    )
    def test_rejects_feature_values_with_wrong_types(
        self, tmp_path: Path, field: str, value: object,
    ) -> None:
        spec: dict[str, object] = {
            "enabled": False,
            "vram_cost_mb": 0,
            "latency_impact_ms": 0,
            "category": "test",
            "depends_on": [],
            "conflicts_with": [],
        }
        spec[field] = value
        loader = write_feature_config(tmp_path, {"demo": spec})
        with pytest.raises(ConfigError, match=f"features.demo.{field}"):
            FeatureManager.from_config(loader)

    def test_rejects_string_resource_budget(self, tmp_path: Path) -> None:
        loader = write_feature_config(tmp_path, {}, total_mb="1000")
        with pytest.raises(ConfigError, match="vram_total_mb"):
            FeatureManager.from_config(loader)

    def test_rejects_enabled_feature_with_disabled_dependency(self, tmp_path: Path) -> None:
        base = {
            "vram_cost_mb": 0,
            "latency_impact_ms": 0,
            "category": "test",
            "conflicts_with": [],
        }
        loader = write_feature_config(tmp_path, {
            "base": {**base, "enabled": False, "depends_on": []},
            "child": {**base, "enabled": True, "depends_on": ["base"]},
        })
        with pytest.raises(ConfigError, match="dependency tắt"):
            FeatureManager.from_config(loader)

    def test_rejects_unknown_dependency(self, tmp_path: Path) -> None:
        loader = write_feature_config(tmp_path, {
            "demo": {
                "enabled": False,
                "vram_cost_mb": 0,
                "latency_impact_ms": 0,
                "category": "test",
                "depends_on": ["missing"],
                "conflicts_with": [],
            },
        })
        with pytest.raises(ConfigError, match="không tồn tại"):
            FeatureManager.from_config(loader)

    def test_rejects_initial_enabled_set_over_budget(self, tmp_path: Path) -> None:
        loader = write_feature_config(tmp_path, {
            "heavy": {
                "enabled": True,
                "vram_cost_mb": 1001,
                "latency_impact_ms": 0,
                "category": "test",
                "depends_on": [],
                "conflicts_with": [],
            },
        })
        with pytest.raises(ConfigError, match="vượt budget"):
            FeatureManager.from_config(loader)
