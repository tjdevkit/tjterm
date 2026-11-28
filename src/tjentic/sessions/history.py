import json
from dataclasses import dataclass
from pathlib import Path

from tjentic.hosts.host import Host


@dataclass(slots=True)
class SubmittedPrompt:
    host: Host
    path: Path
    identity: str
    prompt: str
    offset: int = 0

    def visible(self) -> bool:
        try:
            stream = self.path.open("rb")
        except FileNotFoundError:
            return False
        with stream:
            stream.seek(self.offset)
            while line := stream.readline():
                if not line.endswith(b"\n"):
                    return False
                self.offset = stream.tell()
                try:
                    record: object = json.loads(line)
                except (UnicodeDecodeError, ValueError):
                    continue
                if self._matches(record):
                    return True
        return False

    def _matches(self, record: object) -> bool:
        if not isinstance(record, dict):
            return False
        if self.host is Host.CLAUDE:
            if record.get("type") != "user" or record.get("sessionId") != self.identity:
                return False
            message = record.get("message")
        else:
            if record.get("type") != "response_item":
                return False
            message = record.get("payload")
        if not isinstance(message, dict) or message.get("role") != "user":
            return False
        content = message.get("content")
        if isinstance(content, str):
            return content == self.prompt
        return isinstance(content, list) and any(
            isinstance(part, dict)
            and part.get("type") in {"input_text", "text"}
            and part.get("text") == self.prompt
            for part in content
        )
