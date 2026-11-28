import argparse
import os
import sys
from collections.abc import Sequence
from importlib.metadata import version
from pathlib import Path

from tjentic import standards
from tjentic.errors import TjenticError


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tj", description="TJentic")
    parser.add_argument(
        "--version", action="version", version=f"tj {version('tjentic')}"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    standards.add_command(commands)
    args = parser.parse_args(argv)
    try:
        standards.run(args, Path.cwd(), os.environ)
    except (OSError, TjenticError) as error:
        print(f"tj: {error}", file=sys.stderr)
        return 1
    return 0
