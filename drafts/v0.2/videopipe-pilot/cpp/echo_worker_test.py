"""Transport-only smoke worker. Never used for VLSP/inference comparisons."""
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    denied = request.get("op") == "shutdown" and request.get("deny_shutdown") is True
    print(json.dumps({"id": request["id"], "ok": not denied, "echo": request}), flush=True)
    if request.get("op") == "shutdown" and not denied:
        sys.exit(request.get("exit_code", 0))
