"""
A real MCP server (Model Context Protocol) exposing tools over stdio.
Any MCP client - our LangGraph app, Claude Desktop, Cursor - can use these.

Test standalone:  python -m app.mcp_server
"""
import ast
import json
import operator as op
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("team-tools")
NOTES = Path(__file__).parent.parent / "notes.json"

_OPS = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv,
        ast.Pow: op.pow, ast.USub: op.neg, ast.Mod: op.mod}


def _eval(node):
    # Safe arithmetic evaluator - NEVER use eval() on model-generated text.
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("unsupported expression")


@mcp.tool()
def calculate(expression: str) -> str:
    """Evaluate an arithmetic expression like '(12*7)/3 + 2**5'."""
    try:
        return str(_eval(ast.parse(expression, mode="eval").body))
    except Exception as e:
        return f"error: {e}"


@mcp.tool()
def save_note(title: str, body: str) -> str:
    """Save a note to the shared notebook."""
    data = json.loads(NOTES.read_text()) if NOTES.exists() else {}
    data[title] = body
    NOTES.write_text(json.dumps(data, indent=2))
    return f"saved '{title}'"


@mcp.tool()
def list_notes() -> str:
    """List all saved notes (title: body)."""
    if not NOTES.exists():
        return "no notes yet"
    return json.dumps(json.loads(NOTES.read_text()), indent=2)


@mcp.resource("notes://count")
def notes_count() -> str:
    """Resource: how many notes exist."""
    return str(len(json.loads(NOTES.read_text())) if NOTES.exists() else 0)


if __name__ == "__main__":
    mcp.run(transport="stdio")   # for remote use: transport="streamable-http"
