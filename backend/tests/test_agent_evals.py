"""Trajectory evals: dataset validity, the runner as a gate, and parity with the v1 routing set."""
from __future__ import annotations

import json
from collections import Counter

import pytest

from app.agents_platform.contracts import AgentName
from app.agents_platform.evals.cases import (
    DEFAULT_TRAJECTORIES_PATH,
    load_trajectories,
    parse_trajectories,
)
from app.agents_platform.evals.runner import evaluate, expected_path, main, summarize
from app.agents_platform.orchestration.planner import build_agent_plan
from app.modules.assistant.domain.intents import Intent, classify_by_rules
from app.modules.assistant.evals.cases import EvalDatasetError, load_cases

DEADLINES = {a: 5.0 for a in AgentName}


def test_the_shipped_dataset_loads_and_covers_every_route_and_the_memory_cases():
    cases = load_trajectories()
    tags = Counter(tag for case in cases for tag in case.tags)

    assert len(cases) >= 40
    assert {"single", "multi", "exclusive", "refusal", "memory", "router", "safety"} <= set(tags)
    assert tags["multi"] >= 5 and tags["exclusive"] >= 5 and tags["memory"] >= 8
    assert len({c.id for c in cases}) == len(cases)


async def test_every_trajectory_passes():
    results = await evaluate(load_trajectories())
    failures = {r.id: r.failures for r in results if not r.passed}
    assert not failures, failures


async def test_the_summary_reports_how_much_routing_needed_no_model():
    results = await evaluate(load_trajectories())
    summary = summarize(results)

    assert summary["cases"] == summary["cases_passed"]
    assert summary["turns"] == summary["turns_passed"]
    assert summary["rules_or_memory_routed_share"] > 0.7, "most questions must route with zero router tokens"


def test_the_cli_exits_zero_and_writes_a_report(tmp_path, capsys):
    report = tmp_path / "report.json"
    assert main(["--report", str(report)]) == 0

    body = json.loads(report.read_text())
    assert body["summary"]["cases"] == body["summary"]["cases_passed"]
    assert body["cases"][0]["turns"][0]["path"][0] == "begin_turn"
    assert "trajectory evals:" in capsys.readouterr().out


def test_the_cli_exits_non_zero_when_an_expectation_is_wrong(tmp_path, capsys):
    wrong = tmp_path / "wrong.jsonl"
    wrong.write_text(
        json.dumps(
            {"id": "wrong", "turns": [{"question": "Why is the battery discharging?", "agents": ["knowledge"], "route": "single"}]}
        )
    )
    assert main(["--cases", str(wrong)]) == 1
    assert "agents: expected ['knowledge'], ran ['energy_insight']" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("route", "agents", "expected"),
    [
        ("single", ["energy_insight"], "begin_turn,classify,plan,energy_insight,join,synthesize,output_gate,finalize"),
        ("multi", ["knowledge", "energy_insight"], "begin_turn,classify,plan,energy_insight+knowledge,join,synthesize,output_gate,finalize"),
        ("exclusive", ["device_action"], "begin_turn,classify,plan,device_action,join,synthesize,finalize"),
        ("refusal", [], "begin_turn,classify,plan,refuse,finalize"),
    ],
)
def test_each_route_has_a_fixed_node_sequence(route, agents, expected):
    assert ",".join(expected_path(route, agents)) == expected


# --- dataset validation ---------------------------------------------------------------------------

GOOD_TURN = {"question": "Why is the battery discharging?", "agents": ["energy_insight"], "route": "single"}


def row(**overrides) -> str:
    body = {"id": "c1", "turns": [dict(GOOD_TURN)], **overrides}
    return json.dumps(body)


@pytest.mark.parametrize(
    ("line", "fragment"),
    [
        ("not json", "not valid JSON"),
        ("[]", "must be a JSON object"),
        (json.dumps({"turns": [GOOD_TURN]}), "'id'"),
        (row(turns=[]), "'turns'"),
        (row(extra=1), "unknown keys"),
        (row(turns=[{**GOOD_TURN, "agents": ["telepathy"]}]), "'agents'"),
        (row(turns=[{**GOOD_TURN, "agents": ["energy_insight", "energy_insight"]}]), "'agents'"),
        (row(turns=[{**GOOD_TURN, "route": "sideways"}]), "'route'"),
        (row(turns=[{**GOOD_TURN, "route": "refusal"}]), "refusal runs no agent"),
        (row(turns=[{**GOOD_TURN, "agents": [], "route": "single"}]), "refusal runs no agent"),
        (row(turns=[{**GOOD_TURN, "route": "multi"}]), "'multi'"),
        (row(turns=[{**GOOD_TURN, "intents": ["nonsense"]}]), "'intents'"),
        (row(turns=[{**GOOD_TURN, "method": "vibes"}]), "'method'"),
        (row(turns=[{**GOOD_TURN, "llm": {"router": -1}}]), "'llm'"),
        (row(turns=[{**GOOD_TURN, "llm": {"oracle": 1}}]), "'llm'"),
        (row(turns=[{"question": "q", "route": "single"}]), "missing"),
        (row(turns=[{**GOOD_TURN, "surprise": True}]), "unknown keys"),
    ],
)
def test_a_malformed_case_fails_loudly_with_its_line(line, fragment):
    with pytest.raises(EvalDatasetError) as raised:
        parse_trajectories([line], source="t.jsonl")
    assert fragment in str(raised.value) and "t.jsonl:1" in str(raised.value)


def test_duplicate_ids_are_rejected():
    with pytest.raises(EvalDatasetError, match="duplicate id"):
        parse_trajectories([row(), row()])


def test_blank_lines_and_comments_are_ignored():
    assert len(parse_trajectories(["", "# a comment", row()])) == 1


def test_the_default_path_is_the_shipped_file():
    assert DEFAULT_TRAJECTORIES_PATH.name == "trajectories.jsonl" and DEFAULT_TRAJECTORIES_PATH.exists()


# --- parity with the v1 routing set ---------------------------------------------------------------


@pytest.mark.parametrize("case", load_cases(), ids=lambda c: c.id)
def test_every_v1_routing_case_maps_onto_the_expected_v2_agents(case):
    """v1's cases must route sensibly in v2: control is exclusive, documents go to Knowledge, off-topic is refused."""
    match = classify_by_rules(case.question)
    plan = build_agent_plan(
        question=case.question, intents=match.intents, appliance=None, deadlines=DEADLINES
    )
    agents = [c.agent for c in plan.calls]

    if case.safety in ("refuse_control", "reject_action"):
        assert agents == [AgentName.DEVICE_ACTION] and plan.route == "exclusive"
    elif match.primary is Intent.OUT_OF_SCOPE:
        assert agents == [] and plan.route == "refusal"
    elif match.primary is Intent.DOCUMENT:
        assert agents[0] is AgentName.KNOWLEDGE
    else:
        assert agents[0] is AgentName.ENERGY_INSIGHT and AgentName.DEVICE_ACTION not in agents
