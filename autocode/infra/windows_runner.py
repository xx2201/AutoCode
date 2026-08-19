"""Process entry point for the Windows restricted-token sandbox."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _parse_args(argv: list[str]):
    parser = argparse.ArgumentParser(prog="autocode-windows-sandbox")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--temp-root", required=True)
    parser.add_argument("--mode", choices=("read-only", "workspace-write"), required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command after -- is required")
    return args


def main() -> int:
    args = _parse_args(sys.argv[1:])
    try:
        if __package__:
            from .windows_acl import run_restricted
        else:
            from windows_acl import run_restricted

        return run_restricted(
            args.command,
            Path(args.cwd),
            Path(args.workspace),
            args.mode,
            Path(args.temp_root),
        )
    except Exception as exc:
        print(f"autocode-windows-sandbox: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    raise SystemExit(main())
