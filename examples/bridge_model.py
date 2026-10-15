"""Example external model bridge; replace decision logic with a provider call."""

import json
import sys


request = json.load(sys.stdin)
if any(message["role"] == "tool" for message in request["messages"]):
    response = {"message": "File read.", "finish": True, "usage": {"known": False}}
else:
    response = {
        "tool_calls": [{"name": "read_file", "arguments": {"path": "README.md"}}],
        "usage": {"known": False},
    }
json.dump(response, sys.stdout)
