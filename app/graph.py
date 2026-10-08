"""Assemble the graph. This file is the 'wiring diagram' of the whole system.

   START -> guardrail --blocked--> END
                 |ok
            memory_load -> router --+--> researcher --------------+
                                    +--> coder <-> reviewer (LOOP)-+--> writer -> human_gate -> memory_agent -> END
                                    +--> writer (chat) ------------+
"""
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from app import agents
from app.state import TeamState

# Retry transient failures (rate limits, timeouts) per node, with backoff.
RETRY = RetryPolicy(max_attempts=3, initial_interval=1.0, backoff_factor=2.0)


def build_graph(tools, checkpointer=None, store=None):
    g = StateGraph(TeamState)

    g.add_node("guardrail", agents.guardrail)
    g.add_node("memory_load", agents.memory_load)
    g.add_node("router", agents.router, retry_policy=RETRY)
    g.add_node("researcher", agents.build_researcher(tools), retry_policy=RETRY)
    g.add_node("coder", agents.coder, retry_policy=RETRY)
    g.add_node("reviewer", agents.reviewer, retry_policy=RETRY)
    g.add_node("writer", agents.writer, retry_policy=RETRY)
    g.add_node("human_gate", agents.human_gate)
    g.add_node("memory_agent", agents.memory_agent, retry_policy=RETRY)

    g.add_edge(START, "guardrail")
    # guardrail, router, reviewer route themselves with Command(goto=...)
    g.add_edge("memory_load", "router")
    g.add_edge("researcher", "writer")
    g.add_edge("coder", "reviewer")
    g.add_edge("writer", "human_gate")
    g.add_edge("human_gate", "memory_agent")
    g.add_edge("memory_agent", END)

    # checkpointer = per-thread short-term memory + required for interrupt()
    # store        = cross-thread long-term memory
    return g.compile(checkpointer=checkpointer, store=store)
