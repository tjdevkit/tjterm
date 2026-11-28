import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

from tjentic.errors import TjenticError
from tjentic.hosts.host import Host
from tjentic.watch.native import Watch

_TOOL = {
    "name": "watch_pull_request",
    "description": (
        "When the user authorizes finishing an item on merge and its sole remaining "
        "condition is one GitHub PR, register the wait before ending your turn. "
        "TJ discovers the linked PR from the GitHub issue, verifies the checked-out "
        "revision, and records local closeout automatically even after terminal exit."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "issue": {"type": "string", "description": "Canonical GitHub issue URL"},
            "item": {"type": "string", "description": "Existing TJ item name, if any"},
        },
        "required": ["issue"],
        "additionalProperties": False,
    },
}


def response(
    request: dict[str, object], watch: Watch, directory: Path, session: UUID | None
) -> dict[str, object] | None:
    if "id" not in request:
        return None
    envelope = {"jsonrpc": "2.0", "id": request["id"]}
    params = request.get("params", {})
    if not isinstance(params, dict):
        return {**envelope, "error": {"code": -32602, "message": "Invalid params"}}
    result: dict[str, object]
    match request.get("method"):
        case "initialize":
            supported = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
            protocol = params.get("protocolVersion")
            result = {
                "protocolVersion": protocol if protocol in supported else supported[-1],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "tj-watch", "version": "1"},
            }
        case "ping":
            result = {}
        case "tools/list":
            result = {"tools": [_TOOL]}
        case "tools/call":
            try:
                args = params.get("arguments")
                if params.get("name") != _TOOL["name"] or not isinstance(args, dict):
                    raise TjenticError("Unknown TJ tool or invalid arguments.")
                issue = args.get("issue")
                item = args.get("item", issue)
                if not isinstance(issue, str) or not isinstance(item, str):
                    raise TjenticError("Expected a GitHub issue URL and item name.")
                if session is None:
                    meta = params.get("_meta")
                    if not isinstance(meta, dict) or not isinstance(
                        meta.get("threadId"), str
                    ):
                        raise TjenticError(
                            "Codex did not supply the calling thread ID."
                        )
                    session = UUID(meta["threadId"])
                text = watch.register(session, directory, item, issue)
                failed = False
            except (OSError, TjenticError, ValueError) as error:
                text = str(error)
                failed = True
            result = {"content": [{"type": "text", "text": text}], "isError": failed}
        case _:
            return {
                **envelope,
                "error": {"code": -32601, "message": "Method not found"},
            }
    return {**envelope, "result": result}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", type=Host, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--listener", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--session", type=UUID)
    args = parser.parse_args()
    if args.host == Host.CLAUDE and args.session is None:
        parser.error("Claude requires its launch session ID")
    watch = Watch(args.host, args.state, args.profile, args.listener)
    for line in sys.stdin:
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("invalid request")
            reply = response(message, watch, args.directory, args.session)
        except ValueError:
            reply = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Invalid JSON request"},
            }
        if reply is not None:
            print(json.dumps(reply), flush=True)


if __name__ == "__main__":
    main()
