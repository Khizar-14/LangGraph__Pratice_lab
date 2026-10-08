"""Tool loading: local @tool functions + remote MCP tools, merged into one list."""
import sys
from pathlib import Path

from langchain_core.tools import tool
from langchain_mcp_adapters.client import MultiServerMCPClient

ROOT = Path(__file__).parent.parent


@tool
def word_count(text: str) -> int:
    """Count the words in a piece of text."""
    return len(text.split())


async def load_mcp_tools():
    """Connect to MCP servers. Add more entries to attach more servers.

    stdio  = spawn a local process (above server).
    http   = connect to a remote server, e.g.
             "search": {"url": "https://mcp.example.com/mcp", "transport": "streamable_http",
                        "headers": {"Authorization": "Bearer ..."}}
    """
    client = MultiServerMCPClient({
        "team": {
            "command": sys.executable,
            "args": ["-m", "app.mcp_server"],
            "cwd": str(ROOT),
            "transport": "stdio",
        },
    })
    return await client.get_tools()


async def load_all_tools():
    try:
        mcp_tools = await load_mcp_tools()
    except Exception as e:  # degrade gracefully: app still works without MCP
        print(f"[warn] MCP unavailable, continuing with local tools only: {e}")
        mcp_tools = []
    return [word_count, *mcp_tools]
