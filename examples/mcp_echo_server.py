"""Minimal MCP 2026-07-28 stdio server for harness integration tests."""

import json
import sys


for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    if method == "server/discover":
        result = {"resultType": "complete", "supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}}
    elif method == "tools/list":
        result = {
            "resultType": "complete",
            "tools": [{
                "name": "echo",
                "description": "Echo a short text value",
                "inputSchema": {
                    "type": "object", "properties": {"text": {"type": "string"}},
                    "required": ["text"], "additionalProperties": False,
                },
            }],
        }
    elif method == "tools/call":
        result = {
            "resultType": "complete",
            "content": [{"type": "text", "text": request["params"]["arguments"]["text"]}],
            "isError": False,
        }
    else:
        result = None
    if result is None:
        response = {"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32601, "message": "method not found"}}
    else:
        response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
    print(json.dumps(response), flush=True)
