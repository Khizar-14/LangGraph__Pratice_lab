"""
The 7 agents. Each agent = one node (or subgraph) with ONE job and a model tier
matched to difficulty.

  1. guardrail   (fast)   - block injection / PII before any expensive work
  2. router      (fast)   - structured-output classifier -> research | code | chat
  3. researcher  (smart)  - tool-using loop (subgraph): model <-> tools (MCP)
  4. coder       (smart)  - writes / revises code from feedback
  5. reviewer    (smart)  - structured verdict; drives the coder<->reviewer LOOP
  6. writer      (smart)  - synthesizes the final answer
  7. memory_agent(fast)   - extracts durable facts -> long-term Store

Plus non-LLM nodes: memory_load, human_gate (interrupt).
"""
import re
import uuid
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.store.base import BaseStore
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from app.config import MAX_REVIEW_LOOPS, REQUIRE_APPROVAL, get_llm
from app.state import TeamState


# ------------------------------------------------------------------ helpers
def text_of(msg) -> str:
    """Message content can be a str or a list of blocks (Anthropic). Normalize."""
    c = msg.content
    if isinstance(c, str):
        return c
    return "".join(b.get("text", "") for b in c if isinstance(b, dict))


def last_user_text(state: TeamState) -> str:
    for m in reversed(state["messages"]):
        if isinstance(m, HumanMessage) or getattr(m, "type", "") == "human":
            return text_of(m)
    return ""


# ------------------------------------------------------------- 1. GUARDRAIL
INJECTION = re.compile(
    r"(ignore (all )?(previous|prior) instructions|reveal (your )?system prompt|"
    r"disregard .* rules)", re.I)
PII = re.compile(r"\b(\d{3}-\d{2}-\d{4}|\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{4})\b")


async def guardrail(state: TeamState) -> Command[Literal["memory_load", "__end__"]]:
    text = last_user_text(state)
    if INJECTION.search(text) or PII.search(text):
        return Command(
            update={"blocked": True, "trace": ["guardrail:BLOCKED"],
                    "final_answer": "I can't process that request (policy check failed).",
                    "messages": [AIMessage(content="I can't process that request (policy check failed).")]},
            goto=END)
    return Command(update={"blocked": False, "trace": ["guardrail:ok"]}, goto="memory_load")


# ------------------------------------------------------------ memory (load)
async def memory_load(state: TeamState, *, store: BaseStore) -> dict:
    """Long-term memory: pull what we know about this user from the Store."""
    ns = ("memories", state.get("user_id", "anon"))
    items = await store.asearch(ns, limit=10)
    return {"memories": [i.value["fact"] for i in items], "trace": [f"memory_load:{len(items)}"]}


# --------------------------------------------------------------- 2. ROUTER
class RouteDecision(BaseModel):
    route: Literal["research", "code", "chat"] = Field(
        description="research=needs facts/tools/calculation; code=write or fix code; chat=simple conversation")
    reason: str


async def router(state: TeamState) -> Command[Literal["researcher", "coder", "writer"]]:
    llm = get_llm("fast").with_structured_output(RouteDecision)
    d: RouteDecision = await llm.ainvoke([
        SystemMessage(content="Classify the user's request. Known user facts: "
                              f"{state.get('memories', [])}"),
        HumanMessage(content=last_user_text(state))])
    target = {"research": "researcher", "code": "coder", "chat": "writer"}[d.route]
    return Command(update={"route": d.route, "route_reason": d.reason,
                           "trace": [f"router:{d.route}"]}, goto=target)


# ----------------------------------------------------------- 3. RESEARCHER
def build_researcher(tools):
    """A SUBGRAPH with its own private loop: model -> (tool calls) -> model ..."""
    llm = get_llm("smart").bind_tools(tools)

    async def call_model(state: MessagesState):
        return {"messages": [await llm.ainvoke(state["messages"])]}

    g = StateGraph(MessagesState)
    g.add_node("model", call_model)
    g.add_node("tools", ToolNode(tools, handle_tool_errors=True))  # errors go back to the LLM
    g.add_edge(START, "model")
    g.add_conditional_edges("model", tools_condition)   # tool_calls? -> "tools" else END
    g.add_edge("tools", "model")                         # THE LOOP
    sub = g.compile()

    async def researcher(state: TeamState) -> dict:
        out = await sub.ainvoke(
            {"messages": [SystemMessage(content=(
                "You are a research agent. Use tools for any calculation or note lookup. "
                f"User facts: {state.get('memories', [])}. Return concise findings.")),
                HumanMessage(content=last_user_text(state))]},
            config={"recursion_limit": 12})              # hard cap on tool loops
        return {"research_notes": text_of(out["messages"][-1]), "trace": ["researcher:done"]}

    return researcher


# ---------------------------------------------------------------- 4. CODER
async def coder(state: TeamState) -> dict:
    llm = get_llm("smart")
    feedback = state.get("review_feedback", "")
    prompt = f"Task: {last_user_text(state)}\n"
    if feedback:
        prompt += f"\nYour previous code:\n{state.get('code', '')}\n\nReviewer feedback to fix:\n{feedback}\n"
    resp = await llm.ainvoke([
        SystemMessage(content="You are a senior engineer. Return ONLY complete, runnable code."),
        HumanMessage(content=prompt)])
    n = state.get("iterations", 0) + 1
    return {"code": text_of(resp), "iterations": n, "trace": [f"coder:v{n}"]}


# -------------------------------------------------------------- 5. REVIEWER
class Review(BaseModel):
    approved: bool
    feedback: str = Field(description="Concrete fixes needed; empty if approved")


async def reviewer(state: TeamState) -> Command[Literal["coder", "writer"]]:
    llm = get_llm("smart").with_structured_output(Review)
    r: Review = await llm.ainvoke([
        SystemMessage(content="Strict code reviewer. Check correctness, edge cases, security."),
        HumanMessage(content=f"Task: {last_user_text(state)}\n\nCode:\n{state['code']}")])
    out_of_budget = state.get("iterations", 0) >= MAX_REVIEW_LOOPS      # LOOP EXIT #2
    done = r.approved or out_of_budget                                  # LOOP EXIT #1
    return Command(
        update={"approved": r.approved, "review_feedback": r.feedback,
                "trace": [f"reviewer:{'approve' if r.approved else 'reject'}"]},
        goto="writer" if done else "coder")


# ----------------------------------------------------------------- 6. WRITER
async def writer(state: TeamState) -> dict:
    llm = get_llm("smart")
    ctx = []
    if state.get("research_notes"):
        ctx.append(f"Research notes:\n{state['research_notes']}")
    if state.get("code"):
        flag = "" if state.get("approved") else "\n(NOTE: reviewer limit hit; mention caveats.)"
        ctx.append(f"Final code:\n{state['code']}{flag}")
    resp = await llm.ainvoke([
        SystemMessage(content="Write the final answer for the user. Be clear and concise. "
                              f"Known user facts: {state.get('memories', [])}"),
        *state["messages"],
        HumanMessage(content="\n\n".join(ctx) or "Answer the user directly.")])
    return {"draft": text_of(resp), "trace": ["writer:done"]}


# ------------------------------------------------------- human approval gate
async def human_gate(state: TeamState) -> dict:
    """Pauses the graph (state is checkpointed) until the API resumes it."""
    draft = state["draft"]
    if REQUIRE_APPROVAL:
        decision = interrupt({"type": "approval", "draft": draft,
                              "instructions": 'Resume with {"approve": true} or {"approve": false, "edit": "..."}'})
        if not decision.get("approve", False):
            draft = decision.get("edit") or "The response was rejected by a human reviewer."
    return {"final_answer": draft, "messages": [AIMessage(content=draft)], "trace": ["human_gate"]}


# ------------------------------------------------------ 7. MEMORY AGENT (save)
class Facts(BaseModel):
    facts: list[str] = Field(description="Durable facts/preferences about the USER only. Empty if none.")


async def memory_agent(state: TeamState, *, store: BaseStore) -> dict:
    llm = get_llm("fast").with_structured_output(Facts)
    out: Facts = await llm.ainvoke([
        SystemMessage(content="Extract long-term facts or preferences the user stated about themselves. "
                              "Ignore task content. Never store secrets or credentials."),
        HumanMessage(content=last_user_text(state))])
    ns = ("memories", state.get("user_id", "anon"))
    for f in out.facts:
        await store.aput(ns, str(uuid.uuid4()), {"fact": f})
    return {"trace": [f"memory_agent:saved {len(out.facts)}"]}
