"""Minimal MCP (Model Context Protocol) server exposing the newsletter tools
over stdio JSON-RPC. Works with any MCP client (Claude Desktop, MCP inspector,
agent frameworks).

Register in an MCP client config like:
  {"command": "python", "args": ["-m", "app.agent.mcp_server"]}
"""
import json
import sys

from .tools import PARAMETERS, DESCRIPTIONS, run_tool

PROTOCOL_VERSION = "2024-11-05"


def _tools_list() -> list:
    return [
        {
            "name": name,
            "description": DESCRIPTIONS.get(name, ""),
            "inputSchema": PARAMETERS[name],
        }
        for name in PARAMETERS
    ]


def handle(request: dict) -> dict | None:
    method = request.get("method")
    req_id = request.get("id")
    notification = req_id is None

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "newsletter-agent", "version": "1.0.0"},
        }
    elif method == "notifications/initialized":
        return None
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": _tools_list()}
    elif method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            value = run_tool(name, arguments)
            payload = json.dumps(value, default=str) if not isinstance(value, str) else value
            result = {"content": [{"type": "text", "text": payload}], "isError": False}
        except Exception as exc:
            result = {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    else:
        if notification:
            return None
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"Unknown method: {method}"}}

    if notification:
        return None
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
