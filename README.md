# LangGraph for AI Engineers: from LLM to a 7-agent production system

## Learning path (do in this order)

| Step | File | What you learn | Needs API key? |
|---|---|---|---|
| 1 | `lessons/01_fundamentals.py` | State, reducers, nodes/edges, conditional routing, loops, `Command`, checkpointer, `interrupt`, `Send`, Store | No |
| 2 | `app/state.py`, `app/config.py` | Shared state design, model tiers (fast vs smart) | No |
| 3 | `app/mcp_server.py`, `app/tools.py` | Build an MCP server, connect it with `MultiServerMCPClient` | No |
| 4 | `app/agents.py` | The 7 agents, structured output, tool loop subgraph | Read only |
| 5 | `app/graph.py` | Wiring, retry policies, checkpointer + store | No |
| 6 | `tests/test_graph_offline.py` | Test a whole agent graph with a fake LLM | No |
| 7 | `run_cli.py` | Talk to the team live | Yes |
| 8 | `app/gateway.py` | FastAPI gateway: auth, rate limit, SSE streaming, resume | Yes |

## Setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
python lessons/01_fundamentals.py          # learn the primitives
python -m tests.test_graph_offline         # prove the wiring works, free
python run_cli.py                          # chat with the team
uvicorn app.gateway:api --port 8000        # production gateway
```

## Architecture
```
START -> guardrail --(blocked)--> END
              | ok
         memory_load -> router --+--> researcher (subgraph: model <-> MCP tools) --+
                                 +--> coder <-> reviewer   (LOOP, max 3)  ---------+--> writer
                                 +--> writer (plain chat) ------------------------+     |
                                                                                    human_gate (interrupt)
                                                                                         |
                                                                                    memory_agent -> END
```

## The 7 agents
1. **guardrail** (fast model / regex) blocks prompt injection and PII before spending tokens.
2. **router** (fast model) uses structured output (`RouteDecision`) to pick research / code / chat.
3. **researcher** (smart) is a subgraph that loops model -> tools -> model using MCP tools.
4. **coder** writes code, and revises it using reviewer feedback.
5. **reviewer** returns a structured `Review`; it decides whether to loop back or finish.
6. **writer** synthesizes the final answer from notes/code/memories.
7. **memory_agent** extracts durable user facts into the long-term Store.

## Production patterns used (and where)
| Pattern | Where | Why |
|---|---|---|
| Routing with `Command(goto=...)` | guardrail, router, reviewer | Node decides state update and next hop together |
| Bounded loops | `reviewer` (MAX_REVIEW_LOOPS) + `recursion_limit` | LLM loops must always have a hard exit |
| Subgraph | `build_researcher` | Private tool loop with its own recursion cap |
| Retry policy | `graph.py` `RETRY` | Survive rate limits/timeouts |
| Tool errors fed back to LLM | `ToolNode(handle_tool_errors=True)` | Model self-corrects instead of crashing |
| Structured output | router, reviewer, memory_agent | No fragile string parsing |
| Model tiering | `get_llm("fast"/"smart")` | Cheap model for classification, strong for reasoning |
| Short-term memory | checkpointer, keyed by `thread_id` | One conversation |
| Long-term memory | Store, namespace `("memories", user_id)` | One user across all conversations |
| Human-in-the-loop | `human_gate` + `/v1/resume` | Approve before the answer ships (`REQUIRE_APPROVAL=true`) |
| MCP | `mcp_server.py` + `tools.py` | Standard tool protocol; swap servers without code changes |
| Gateway | `gateway.py` | API key auth, rate limit, SSE streaming, error masking |
| Persistence | `DATABASE_URL` -> Postgres saver/store | Survives restarts, multi-replica |
| Offline tests | `tests/` | CI without API cost |

## Gateway examples
```bash
curl -X POST localhost:8000/v1/chat -H 'X-API-Key: dev-key' -H 'content-type: application/json' \
  -d '{"message":"I am a python dev. What is (12*7)/3+2**5? use tools","thread_id":"t1","user_id":"u1"}'

# new thread, same user -> long-term memory kicks in
curl -X POST localhost:8000/v1/chat ... -d '{"message":"what do you know about me?","thread_id":"t2","user_id":"u1"}'

# human approval flow
REQUIRE_APPROVAL=true uvicorn app.gateway:api
# -> {"status":"needs_approval", ...}  then:
curl -X POST localhost:8000/v1/resume ... -d '{"thread_id":"t1","approve":true}'
```

## Before real production (checklist)
- Run code in a sandbox (E2B / Docker) if the coder's output is ever executed. Never `exec` it in-process.
- Replace the in-memory rate limiter with Redis; put the gateway behind a real auth provider (JWT/OIDC).
- Add tracing: set `LANGSMITH_TRACING=true` + `LANGSMITH_API_KEY`.
- Add semantic memory search: create the Store with an `index={"embed": ..., "dims": ...}`.
- Add evals: a golden set of prompts, assert on route + answer quality (LangSmith or your own).
- Context control: trim/summarize `messages` when threads get long (`trim_messages`).
- Secrets only via env/secret manager; MCP servers get least-privilege credentials.

## Exercises (this is how it sticks)
1. Add an 8th agent, **tester**, that runs the code in a sandbox and feeds failures back to the coder.
2. Change `researcher` to fan out with `Send`: one worker per sub-question, then merge.
3. Add a `/v1/threads/{id}/history` endpoint using `graph.aget_state_history`.
4. Add a second MCP server over HTTP (e.g. a search tool) in `tools.py`.
5. Add semantic memory search so `memory_load` retrieves only facts relevant to the question.
6. Write a test asserting the guardrail blocks a credit-card number.
