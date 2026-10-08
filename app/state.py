"""Shared graph state. One TypedDict that every agent reads/writes."""
import operator
from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages


class TeamState(TypedDict, total=False):
    # conversation (add_messages reducer: appends + dedupes by message id)
    messages: Annotated[list, add_messages]

    # identity (used to namespace long-term memory)
    user_id: str

    # routing
    blocked: bool            # set by guardrail
    route: str               # research | code | chat
    route_reason: str

    # long-term memory loaded for this run
    memories: list[str]

    # code <-> review loop
    code: str
    review_feedback: str
    approved: bool
    iterations: int

    # research output
    research_notes: str

    # final
    draft: str
    final_answer: str

    # observability: every agent appends a breadcrumb (reducer = append)
    trace: Annotated[list[str], operator.add]
