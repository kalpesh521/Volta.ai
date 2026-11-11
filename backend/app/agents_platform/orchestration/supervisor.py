"""
The supervisor graph: classify, plan, dispatch agents in parallel, synthesize, gate, finalize.

Agents return to the supervisor and never to each other. Only the supervisor dispatches, so a plan is
bounded (at most `MAX_AGENTS_PER_PLAN` agents, one regeneration) and every step is auditable.

Compiled once per process with the shared checkpointer. One conversation is one thread, so the
checkpoint is also the conversation memory. Per-request dependencies arrive through
`context=SupervisorRuntime(...)`.
"""
from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents_platform.contracts import AgentName
from app.agents_platform.orchestration import nodes
from app.agents_platform.orchestration.runtime import SupervisorRuntime
from app.agents_platform.orchestration.state import AgentInput, SupervisorState

GRAPH_NAME = "suryaa_supervisor"
RECURSION_LIMIT = 25


def build_supervisor(checkpointer: BaseCheckpointSaver | None) -> CompiledStateGraph:
    graph = StateGraph(SupervisorState, context_schema=SupervisorRuntime)

    graph.add_node(nodes.BEGIN_TURN, nodes.begin_turn)
    graph.add_node(nodes.CLASSIFY, nodes.classify)
    graph.add_node(nodes.PLAN, nodes.plan)
    for agent in AgentName:
        graph.add_node(agent.value, nodes.make_agent_node(agent), input_schema=AgentInput)
    graph.add_node(nodes.JOIN, nodes.join, defer=True)  # waits for every dispatched branch
    graph.add_node(nodes.SYNTHESIZE, nodes.synthesize)
    graph.add_node(nodes.OUTPUT_GATE, nodes.output_gate)
    graph.add_node(nodes.FALLBACK, nodes.deterministic_fallback)
    graph.add_node(nodes.REFUSE, nodes.refuse)
    graph.add_node(nodes.FINALIZE, nodes.finalize)

    graph.add_edge(START, nodes.BEGIN_TURN)
    graph.add_edge(nodes.BEGIN_TURN, nodes.CLASSIFY)
    graph.add_edge(nodes.CLASSIFY, nodes.PLAN)
    graph.add_conditional_edges(
        nodes.PLAN, nodes.dispatch, [nodes.REFUSE, *(agent.value for agent in AgentName)]
    )
    # Every agent rejoins the same way. The exclusive route differs only in that the planner never
    # sends it beside another agent, and its draft passes straight through synthesis.
    for agent in AgentName:
        graph.add_edge(agent.value, nodes.JOIN)
    graph.add_edge(nodes.JOIN, nodes.SYNTHESIZE)
    graph.add_conditional_edges(
        nodes.SYNTHESIZE,
        nodes.route_after_synthesize,
        {nodes.OUTPUT_GATE: nodes.OUTPUT_GATE, nodes.FINALIZE: nodes.FINALIZE},
    )
    graph.add_conditional_edges(
        nodes.OUTPUT_GATE,
        nodes.route_after_gate,
        {nodes.SYNTHESIZE: nodes.SYNTHESIZE, nodes.FALLBACK: nodes.FALLBACK, nodes.FINALIZE: nodes.FINALIZE},
    )
    graph.add_edge(nodes.FALLBACK, nodes.FINALIZE)
    graph.add_edge(nodes.REFUSE, nodes.FINALIZE)
    graph.add_edge(nodes.FINALIZE, END)

    return graph.compile(checkpointer=checkpointer, name=GRAPH_NAME)
