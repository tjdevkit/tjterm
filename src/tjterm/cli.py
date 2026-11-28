import argparse
import os
import sys
from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path

from tjterm import standards


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tj", description="TJentic")
    parser.add_argument(
        "--version", action="version", version=f"tj {version('tjterm')}"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    standards.add_command(commands)
    args = parser.parse_args(argv)
    try:
        standards.run_sync(Path.cwd(), apply=not args.dry_run, environment=os.environ)
    except (OSError, standards.StandardsError) as error:
        print(f"tj: {error}", file=sys.stderr)
        return 1
    return 0
