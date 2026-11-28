"""A local Codex process double for history pages and native resume arguments."""

import json
import sys
from pathlib import Path


def main() -> int:
    scenario = Path(sys.argv[1])
    config = json.loads(scenario.read_text(encoding="utf-8"))
    log = scenario.with_suffix(".log")
    if sys.argv[2] == "resume":
        with log.open("a", encoding="utf-8") as stream:
            print(
                json.dumps({"argv": sys.argv[2:], "cwd": str(Path.cwd())}), file=stream
            )
        return int(config.get("exit_code", 0))
    assert sys.argv[2:] == ["app-server"]
    for line in sys.stdin:
        msg = json.loads(line)
        method = msg["method"]
        with log.open("a", encoding="utf-8") as stream:
            print(json.dumps(msg), file=stream)
        if method == "initialized":
            continue
        params = msg["params"]
        payload: dict[str, object]
        if method == "initialize":
            payload = {"result": {"userAgent": "synthetic"}}
        elif method == "thread/read":
            assert set(params) == {"threadId"}
            payload = {"result": {"thread": config["thread"]}}
        elif "error" in config:
            payload = {"error": {"code": -32601, "message": config["error"]}}
        else:
            assert params["currentAccountOnly"] is True
            pages = config["pages"]
            index = int(params.get("cursor", 0))
            rows = pages[index]
            if method == "thread/search":
                assert params["searchTerm"] == config["query"]
                rows = [{"thread": row, "snippet": "synthetic hit"} for row in rows]
            else:
                assert method == "thread/list"
                assert params["modelProviders"] == []
            cursor = str(index + 1) if index + 1 < len(pages) else None
            payload = {"result": {"data": rows, "nextCursor": cursor}}
        print(json.dumps({"id": msg["id"], **payload}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
