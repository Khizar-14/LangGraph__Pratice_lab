"""
Production gateway: FastAPI in front of the graph.

  POST /v1/chat          -> run to completion (or to a human-approval pause)
  POST /v1/chat/stream   -> Server-Sent Events: live per-agent progress
  POST /v1/resume        -> continue a paused (human-in-the-loop) run
  GET  /v1/threads/{id}  -> inspect saved state
  GET  /health

Run:  uvicorn app.gateway:api --port 8000
Auth: header  X-API-Key: dev-key
"""
import json
import time
from collections import defaultdict, deque
from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from pydantic import BaseModel

from app.config import DATABASE_URL, GATEWAY_API_KEYS, RATE_LIMIT_PER_MIN
from app.graph import build_graph
from app.tools import load_all_tools

STATE: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    stack = AsyncExitStack()
    if DATABASE_URL:   # PRODUCTION persistence: survives restarts, shared across replicas
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from langgraph.store.postgres.aio import AsyncPostgresStore
        checkpointer = await stack.enter_async_context(AsyncPostgresSaver.from_conn_string(DATABASE_URL))
        store = await stack.enter_async_context(AsyncPostgresStore.from_conn_string(DATABASE_URL))
        await checkpointer.setup()
        await store.setup()
    else:              # DEV: in-memory, lost on restart
        checkpointer, store = InMemorySaver(), InMemoryStore()
    tools = await load_all_tools()
    STATE["graph"] = build_graph(tools, checkpointer, store)
    yield
    await stack.aclose()


api = FastAPI(title="LangGraph Team Gateway", lifespan=lifespan)

# ----------------------------------------------------------- auth + rate limit
_hits: dict[str, deque] = defaultdict(deque)


def authenticate(x_api_key: str = Header(default="")) -> str:
    if x_api_key not in GATEWAY_API_KEYS:
        raise HTTPException(401, "invalid API key")
    q, now = _hits[x_api_key], time.time()
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_LIMIT_PER_MIN:
        raise HTTPException(429, "rate limit exceeded")
    q.append(now)
    return x_api_key


# ---------------------------------------------------------------------- models
class ChatIn(BaseModel):
    message: str
    thread_id: str          # one conversation
    user_id: str = "anon"   # one person (long-term memory key)


class ResumeIn(BaseModel):
    thread_id: str
    approve: bool = True
    edit: str | None = None


def cfg(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": 40,
            "tags": ["gateway"], "metadata": {"thread_id": thread_id}}


def shape(result: dict) -> dict:
    if "__interrupt__" in result:
        return {"status": "needs_approval", "interrupt": result["__interrupt__"][0].value}
    return {"status": "done", "answer": result.get("final_answer"), "trace": result.get("trace")}


# ------------------------------------------------------------------- endpoints
@api.get("/health")
async def health():
    return {"ok": "graph" in STATE}


@api.post("/v1/chat")
async def chat(body: ChatIn, _: str = Depends(authenticate)):
    try:
        res = await STATE["graph"].ainvoke(
            {"messages": [HumanMessage(content=body.message)], "user_id": body.user_id,
             "iterations": 0, "review_feedback": "", "code": "", "research_notes": ""},
            cfg(body.thread_id))
    except Exception as e:   # never leak stack traces to clients
        raise HTTPException(500, f"agent error: {type(e).__name__}")
    return shape(res)


@api.post("/v1/chat/stream")
async def chat_stream(body: ChatIn, _: str = Depends(authenticate)):
    async def events():
        async for chunk in STATE["graph"].astream(
                {"messages": [HumanMessage(content=body.message)], "user_id": body.user_id,
                 "iterations": 0, "review_feedback": "", "code": "", "research_notes": ""},
                cfg(body.thread_id), stream_mode="updates"):
            for node, update in chunk.items():
                if node == "__interrupt__":
                    yield f"data: {json.dumps({'event': 'needs_approval', 'data': str(update[0].value)})}\n\n"
                else:
                    yield f"data: {json.dumps({'event': 'agent_done', 'agent': node, 'trace': (update or {}).get('trace')})}\n\n"
        yield "data: {\"event\": \"end\"}\n\n"
    return StreamingResponse(events(), media_type="text/event-stream")


@api.post("/v1/resume")
async def resume(body: ResumeIn, _: str = Depends(authenticate)):
    res = await STATE["graph"].ainvoke(
        Command(resume={"approve": body.approve, "edit": body.edit}), cfg(body.thread_id))
    return shape(res)


@api.get("/v1/threads/{thread_id}")
async def thread_state(thread_id: str, _: str = Depends(authenticate)):
    s = await STATE["graph"].aget_state(cfg(thread_id))
    return {"next": list(s.next), "trace": s.values.get("trace"), "route": s.values.get("route")}
