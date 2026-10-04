"""
LangGraph wiring for the read-only assistant.

    START → classify → select_tools → call_tools → analyze → generate → finalize → END
    select_tools → analyze            when the plan has no tools (blocked intents)
    analyze      → fallback           no data / blocked intent / no LLM configured
    generate     → fallback           LLM error or unparseable output
    fallback     → finalize

Compiled once per process; per-request dependencies arrive via
`context=AssistantRuntime(...)` on `ainvoke`.
"""
from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.modules.assistant.graph import nodes
from app.modules.assistant.graph.state import AssistantRuntime, AssistantState

GRAPH_NAME = "suryaa_assistant"


def build_assistant_graph() -> CompiledStateGraph:
    graph = StateGraph(AssistantState, context_schema=AssistantRuntime)

    graph.add_node(nodes.CLASSIFY, nodes.classify)
    graph.add_node(nodes.SELECT_TOOLS, nodes.select_tools)
    graph.add_node(nodes.CALL_TOOLS, nodes.call_tools)
    graph.add_node(nodes.ANALYZE, nodes.analyze)
    graph.add_node(nodes.GENERATE, nodes.generate)
    graph.add_node(nodes.FALLBACK, nodes.fallback)
    graph.add_node(nodes.FINALIZE, nodes.finalize)

    graph.add_edge(START, nodes.CLASSIFY)
    graph.add_edge(nodes.CLASSIFY, nodes.SELECT_TOOLS)
    graph.add_conditional_edges(
        nodes.SELECT_TOOLS,
        nodes.route_after_select,
        {nodes.CALL_TOOLS: nodes.CALL_TOOLS, nodes.ANALYZE: nodes.ANALYZE},
    )
    graph.add_edge(nodes.CALL_TOOLS, nodes.ANALYZE)
    graph.add_conditional_edges(
        nodes.ANALYZE,
        nodes.route_after_analyze,
        {nodes.GENERATE: nodes.GENERATE, nodes.FALLBACK: nodes.FALLBACK},
    )
    graph.add_conditional_edges(
        nodes.GENERATE,
        nodes.route_after_generate,
        {nodes.FALLBACK: nodes.FALLBACK, nodes.FINALIZE: nodes.FINALIZE},
    )
    graph.add_edge(nodes.FALLBACK, nodes.FINALIZE)
    graph.add_edge(nodes.FINALIZE, END)

    return graph.compile(name=GRAPH_NAME)
