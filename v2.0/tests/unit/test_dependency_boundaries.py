"""Dependency and compatibility guards for canonical public contracts."""
from __future__ import annotations

import ast
from pathlib import Path

from interfaces import evaluation as canonical_evaluation
from interfaces import relationship as canonical_relationship
from interfaces import state as canonical_state
from services.agent import behavior_library, goal_proposal, goal_types, thread_extraction, types
from services.evaluation import types as evaluation_types
from services.relationship import types as relationship_types


ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_INTERFACE_ROOTS = {"orchestrator", "services"}
LEGACY_TYPE_MODULES = {
    "services.agent.goal_types",
    "services.agent.types",
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


def test_legacy_agent_symbols_are_exact_canonical_objects() -> None:
    state_names = (
        "AgentStateSnapshot", "ConversationMove", "Goal", "GoalKind", "GoalSnapshot",
        "GoalSource", "GoalStatus", "OpenThread", "SessionRecap", "ShortIntention",
        "ShortIntentionStatus", "StreamPhase", "ThreadEvidence", "ThreadKind",
        "ThreadOperation", "ThreadSignal", "ThreadStatus", "TopicMatch", "TopicState",
    )
    for name in state_names:
        legacy = getattr(goal_types, name, None) or getattr(types, name, None)
        assert legacy is getattr(canonical_state, name), name
    assert goal_proposal.GoalProposal is canonical_state.GoalProposal
    assert thread_extraction.ThreadExtraction is canonical_state.ThreadExtraction
    assert behavior_library.BehaviorKind is canonical_state.BehaviorKind
    assert behavior_library.BehaviorDecision is canonical_state.BehaviorDecision


def test_legacy_relationship_and_evaluation_symbols_are_exact_objects() -> None:
    for name in relationship_types.__all__:
        assert getattr(relationship_types, name) is getattr(canonical_relationship, name), name
    for name in evaluation_types.__all__:
        assert getattr(evaluation_types, name) is getattr(canonical_evaluation, name), name


def test_strict_proposal_shapes_survive_compatibility_imports() -> None:
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
