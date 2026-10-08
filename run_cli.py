"""CLI runner: talk to the team from the terminal (no gateway needed).

  export ANTHROPIC_API_KEY=...
  python run_cli.py
Try:  "I'm a Python dev at a bank. What is (12*7)/3 + 2**5? use your tools"
      "write a function that validates IBAN numbers"
      then in a NEW thread ask: "what do you know about me?"
"""
import asyncio

from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from app.graph import build_graph
from app.tools import load_all_tools


async def main():
    graph = build_graph(await load_all_tools(), InMemorySaver(), InMemoryStore())
    thread = "cli-1"
    while True:
        q = input("\nyou> ").strip()
        if q in {"exit", "quit"}:
            break
        if q.startswith("/thread "):          # switch thread: short-term memory resets,
            thread = q.split(maxsplit=1)[1]    # long-term (same user_id) persists
            continue
        out = await graph.ainvoke(
            {"messages": [HumanMessage(content=q)], "user_id": "me", "iterations": 0},
            {"configurable": {"thread_id": thread}, "recursion_limit": 40})
        print("\n[trace]", " -> ".join(out.get("trace", [])))
        print("\n" + out.get("final_answer", ""))


if __name__ == "__main__":
    asyncio.run(main())
