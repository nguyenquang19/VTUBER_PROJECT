"""Dependency and compatibility guards for canonical public contracts."""
from __future__ import annotations

import ast
from pathlib import Path

from interfaces import state as canonical_state
from services.agent import behavior_library, goal_proposal, thread_extraction


ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_INTERFACE_ROOTS = {"orchestrator", "services"}
LEGACY_TYPE_MODULES = {
    "services.agent.types",
    "services.agent.goal_types",
    "services.evaluation.types",
    "services.relationship.types",
}


def _imports(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    return tuple(imported)


def _interface_module(path: Path) -> str:
    relative = path.relative_to(ROOT).with_suffix("")
    return ".".join(relative.parts)


def test_interfaces_never_import_implementation_or_orchestrator() -> None:
    violations: list[str] = []
    for path in sorted((ROOT / "interfaces").glob("*.py")):
        for imported in _imports(path):
            if imported.split(".", 1)[0] in FORBIDDEN_INTERFACE_ROOTS:
                violations.append(f"{path.relative_to(ROOT).as_posix()} -> {imported}")
    assert violations == []


def test_interface_import_graph_has_no_cycle() -> None:
    paths = tuple(sorted((ROOT / "interfaces").glob("*.py")))
    modules = {_interface_module(path): path for path in paths}
    graph = {
        module: {
            imported for imported in _imports(path)
            if imported in modules and imported != module
        }
        for module, path in modules.items()
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module: str, trail: tuple[str, ...]) -> None:
        if module in visiting:
            start = trail.index(module)
            raise AssertionError("interface import cycle: " + " -> ".join(trail[start:] + (module,)))
        if module in visited:
            return
        visiting.add(module)
        for dependency in sorted(graph[module]):
            visit(dependency, trail + (module,))
        visiting.remove(module)
        visited.add(module)

    for module in sorted(graph):
        visit(module, ())


def test_production_consumers_use_canonical_contract_imports() -> None:
    violations: list[str] = []
    for directory in ("orchestrator", "services", "scripts"):
        for path in sorted((ROOT / directory).rglob("*.py")):
            for imported in _imports(path):
                if imported in LEGACY_TYPE_MODULES:
                    violations.append(f"{path.relative_to(ROOT).as_posix()} -> {imported}")
    assert violations == []


def test_implementation_re_exports_are_removed() -> None:
    retired = (
        "services/agent/types.py",
        "services/agent/goal_types.py",
        "services/evaluation/types.py",
        "services/relationship/types.py",
    )
    assert [path for path in retired if (ROOT / path).exists()] == []


def test_implementation_modules_use_canonical_state_objects() -> None:
    assert goal_proposal.GoalProposal is canonical_state.GoalProposal
    assert thread_extraction.ThreadExtraction is canonical_state.ThreadExtraction
    assert behavior_library.BehaviorKind is canonical_state.BehaviorKind
    assert behavior_library.BehaviorDecision is canonical_state.BehaviorDecision


def test_strict_proposal_shapes_survive_canonical_imports() -> None:
    proposal = goal_proposal.GoalProposal.model_validate(
        {
            "kind": "continue_thread",
            "reason": "  tiếp tục   câu chuyện ",
            "success_condition": " có phản hồi ",
            "source_event_id": " event-1 ",
        }
    )
    assert proposal is not None
    assert proposal.model_dump(mode="json") == {
        "kind": "continue_thread",
        "reason": "tiếp tục câu chuyện",
        "success_condition": "có phản hồi",
        "source_event_id": "event-1",
        "parent_thread_id": None,
    }


def test_live_entrypoint_graph_does_not_reach_offline_evaluation() -> None:
    roots = ("interfaces", "orchestrator", "services", "dashboard", "scripts")
    modules: dict[str, Path] = {}
    for root in roots:
        for path in (ROOT / root).rglob("*.py"):
            relative = path.relative_to(ROOT).with_suffix("")
            parts = relative.parts[:-1] if relative.name == "__init__" else relative.parts
            modules[".".join(parts)] = path

    graph: dict[str, set[str]] = {}
    for module, path in modules.items():
        dependencies: set[str] = set()
        for imported in _imports(path):
            if imported in modules:
                dependencies.add(imported)
            parts = imported.split(".")
            for index in range(len(parts), 0, -1):
                candidate = ".".join(parts[:index])
                if candidate in modules:
                    dependencies.add(candidate)
                    break
        graph[module] = dependencies

    pending = ["scripts.stream_youtube", "scripts.stream_discord"]
    reachable: set[str] = set()
    while pending:
        module = pending.pop()
        if module in reachable:
            continue
        reachable.add(module)
        pending.extend(sorted(graph.get(module, ())))
    assert sorted(
        module for module in reachable if module.startswith("services.evaluation")
    ) == []
