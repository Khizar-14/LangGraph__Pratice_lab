"""
LESSON 1 - LangGraph fundamentals (runs OFFLINE, no API key needed).

Run:  python lessons/01_fundamentals.py

Concepts, in order:
  1. State + reducers
  2. Nodes and edges
  3. Conditional routing
  4. Loops (with a hard stop)
  5. Command (route + update in one return)
  6. Checkpointer = short-term memory per thread_id
  7. interrupt() = human-in-the-loop
  8. Send = parallel fan-out (map-reduce)
  9. Store = long-term memory across threads
"""
import operator
from typing import Annotated, Literal, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command, Send, interrupt


def title(t: str) -> None:
    print(f"\n{'=' * 8} {t} {'=' * 8}")


# ---------------------------------------------------------------------------
# 1-2. STATE + NODES + EDGES
# State is a TypedDict. Each key can have a REDUCER that says how to merge
# updates. No reducer = overwrite. operator.add = append lists.
# A node is just a function: state in -> partial state update out.
# ---------------------------------------------------------------------------
class S1(TypedDict):
    text: str                                  # overwrite
    log: Annotated[list[str], operator.add]    # append


def upper(state: S1):
    return {"text": state["text"].upper(), "log": ["upper"]}


def exclaim(state: S1):
    return {"text": state["text"] + "!", "log": ["exclaim"]}


title("1-2 State, nodes, edges")
g = StateGraph(S1)
g.add_node("upper", upper)
g.add_node("exclaim", exclaim)
g.add_edge(START, "upper")
g.add_edge("upper", "exclaim")
g.add_edge("exclaim", END)
app = g.compile()                              # compile() -> runnable
print(app.invoke({"text": "hello", "log": []}))


# ---------------------------------------------------------------------------
# 3. CONDITIONAL ROUTING
# A router function returns the NAME of the next node. This is how an LLM
# "supervisor" decides which agent runs next (the router just calls an LLM).
# ---------------------------------------------------------------------------
class S3(TypedDict):
    question: str
    answer: str


def classify(state: S3) -> Literal["math", "chat"]:
    return "math" if any(c.isdigit() for c in state["question"]) else "chat"


title("3 Conditional routing")
g = StateGraph(S3)
g.add_node("math", lambda s: {"answer": "doing math"})
g.add_node("chat", lambda s: {"answer": "just chatting"})
g.add_conditional_edges(START, classify)       # router decides
g.add_edge("math", END)
g.add_edge("chat", END)
app = g.compile()
print(app.invoke({"question": "2+2?"}), app.invoke({"question": "hi"}))


# ---------------------------------------------------------------------------
# 4. LOOPS - writer/reviewer pattern. ALWAYS have a max-iteration exit,
# otherwise an LLM that never "approves" loops forever and burns money.
# ---------------------------------------------------------------------------
class S4(TypedDict):
    draft: str
    tries: int


def write(state: S4):
    n = state.get("tries", 0) + 1
    return {"draft": "x" * n, "tries": n}


def review(state: S4) -> Literal["write", "__end__"]:
    good_enough = len(state["draft"]) >= 3
    out_of_budget = state["tries"] >= 5       # safety valve
    return END if (good_enough or out_of_budget) else "write"


title("4 Loops")
g = StateGraph(S4)
g.add_node("write", write)
g.add_edge(START, "write")
g.add_conditional_edges("write", review)
print(g.compile().invoke({"draft": "", "tries": 0}))


# ---------------------------------------------------------------------------
# 5. COMMAND - a node returns BOTH the state update AND where to go next.
# This is what we use for every agent in the real system.
# ---------------------------------------------------------------------------
class S5(TypedDict):
    n: int
    path: Annotated[list[str], operator.add]


def a(state: S5) -> Command[Literal["b", "c"]]:
    return Command(update={"path": ["a"]}, goto="b" if state["n"] > 0 else "c")


title("5 Command")
g = StateGraph(S5)
g.add_node("a", a)
g.add_node("b", lambda s: {"path": ["b"]})
g.add_node("c", lambda s: {"path": ["c"]})
g.add_edge(START, "a")
g.add_edge("b", END)
g.add_edge("c", END)
app = g.compile()
print(app.invoke({"n": 1, "path": []}), app.invoke({"n": 0, "path": []}))


# ---------------------------------------------------------------------------
# 6. CHECKPOINTER = SHORT-TERM MEMORY
# Same thread_id => graph resumes from saved state. Different id => fresh.
# Prod: PostgresSaver. Dev: InMemorySaver.
# ---------------------------------------------------------------------------
class S6(TypedDict):
    history: Annotated[list[str], operator.add]


title("6 Checkpointer (thread memory)")
g = StateGraph(S6)
g.add_node("echo", lambda s: {"history": [f"seen {len(s['history'])} before"]})
g.add_edge(START, "echo")
g.add_edge("echo", END)
app = g.compile(checkpointer=InMemorySaver())
cfg_a = {"configurable": {"thread_id": "alice"}}
cfg_b = {"configurable": {"thread_id": "bob"}}
app.invoke({"history": []}, cfg_a)
print("alice 2nd turn:", app.invoke({"history": []}, cfg_a))
print("bob 1st turn  :", app.invoke({"history": []}, cfg_b))


# ---------------------------------------------------------------------------
# 7. HUMAN-IN-THE-LOOP: interrupt() pauses, state is saved by the
# checkpointer, resume later with Command(resume=value).
# ---------------------------------------------------------------------------
class S7(TypedDict):
    action: str
    result: str


def ask_human(state: S7):
    decision = interrupt({"question": f"Approve '{state['action']}'?"})
    return {"result": "DONE" if decision == "yes" else "CANCELLED"}


title("7 interrupt / resume")
g = StateGraph(S7)
g.add_node("ask_human", ask_human)
g.add_edge(START, "ask_human")
g.add_edge("ask_human", END)
app = g.compile(checkpointer=InMemorySaver())   # REQUIRED for interrupt
cfg = {"configurable": {"thread_id": "t1"}}
r = app.invoke({"action": "delete db", "result": ""}, cfg)
print("paused:", r["__interrupt__"][0].value)
print("resumed:", app.invoke(Command(resume="no"), cfg))


# ---------------------------------------------------------------------------
# 8. SEND = dynamic parallel fan-out (map-reduce). One worker per item.
# ---------------------------------------------------------------------------
class S8(TypedDict):
    topics: list[str]
    notes: Annotated[list[str], operator.add]


def fan_out(state: S8):
    return [Send("worker", {"topic": t}) for t in state["topics"]]


def worker(payload: dict):
    return {"notes": [f"researched {payload['topic']}"]}


title("8 Send (parallel map-reduce)")
g = StateGraph(S8)
g.add_node("worker", worker)
g.add_conditional_edges(START, fan_out, ["worker"])
g.add_edge("worker", END)
print(g.compile().invoke({"topics": ["rag", "mcp", "evals"], "notes": []}))


# ---------------------------------------------------------------------------
# 9. STORE = LONG-TERM MEMORY (across threads and sessions), namespaced.
# Checkpointer remembers a CONVERSATION. Store remembers the USER.
# ---------------------------------------------------------------------------
title("9 Store (long-term memory)")
store = InMemoryStore()
store.put(("memories", "user_42"), "pref1", {"fact": "prefers Python"})
store.put(("memories", "user_42"), "pref2", {"fact": "works at a bank"})
for item in store.search(("memories", "user_42")):
    print(item.key, item.value)
print("other user:", store.search(("memories", "user_99")))

print("\nLesson 1 complete.")
