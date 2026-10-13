#!/usr/bin/python3
"""Block native Electron build commands that bypass the guarded entry point."""

import json
import shlex
import sys
from pathlib import Path
from typing import List, Optional


CONTROL_OPERATORS = {"&&", "||", ";", "|"}
PROTECTED_OUTPUT_NAMES = {
    ".ninja_log",
    ".ninja_deps",
    "args.gn",
    "build.ninja",
    "build.ninja.d",
    "build.ninja.stamp",
    "gn_logs.txt",
    "toolchain.ninja",
}


def shell_segments(command: str) -> List[List[str]]:
    """Split a shell command into simple commands without evaluating it."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    segments = []
    current = []
    for token in lexer:
        if token in CONTROL_OPERATORS:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def executable_index(tokens: List[str]) -> Optional[int]:
    """Find the executable after common assignments and command wrappers."""
    index = 0
    while (
        index < len(tokens)
        and "=" in tokens[index]
        and not tokens[index].startswith("-")
    ):
        index += 1
    while index < len(tokens):
        name = Path(tokens[index]).name
        if name in ("command", "env", "caffeinate", "nohup"):
            index += 1
            while index < len(tokens) and (
                tokens[index].startswith("-") or "=" in tokens[index]
            ):
                index += 1
            continue
        if name == "nice":
            index += 1
            if index < len(tokens) and tokens[index] == "-n":
                index += 2
            continue
        return index
    return None


def denial_reason(command: str) -> Optional[str]:
    """Return a reason when a Bash command violates native-build policy."""
    try:
        segments = shell_segments(command)
    except ValueError:
        return "Could not safely parse the shell command."
    guarded_segments = []
    for tokens in segments:
        index = executable_index(tokens)
        if index is None:
            continue
        executable = Path(tokens[index]).name
        arguments = tokens[index + 1 :]
        if executable in ("ninja", "autoninja", "autoninja.py", "ninja.py"):
            return "Raw Ninja/autoninja is prohibited. Use the guarded entry point."
        if executable == "e" and arguments[:1] == ["build"]:
            return "Raw e build is prohibited. Use the guarded entry point."
        if executable in ("gn", "gn.py") and arguments[:1] in (["gen"], ["clean"]):
            return "GN generation/cleaning is prohibited in agent workflows."
        if executable == "rm" and any(
            Path(argument.rstrip("/")).name in PROTECTED_OUTPUT_NAMES
            or "/out/" in argument
            or argument.rstrip("/").endswith("/out")
            for argument in arguments
            if not argument.startswith("-")
        ):
            return (
                "Deleting or invalidating Electron native build metadata is prohibited."
            )
        if (
            executable.startswith("python")
            and "-m" not in arguments
            and any(
                Path(argument).name == "build_electron.py" for argument in arguments
            )
        ):
            return "Direct Python entry-point invocation is prohibited; use build-electron.sh."
        if executable == "build_electron.py":
            return "Direct Python entry-point invocation is prohibited; use build-electron.sh."
        if executable == "build-electron.sh":
            guarded_segments.append(tokens)

    if not guarded_segments:
        return None
    if len(segments) != 1 or len(guarded_segments) != 1:
        return "The guarded build command must be invoked directly, without shell chaining."
    tokens = guarded_segments[0]
    if "--regenerate-only" in tokens:
        return "Codex is prohibited from regenerating Electron build metadata."
    if "--mode" not in tokens:
        return "Codex must invoke the guarded build entry point with --mode codex."
    mode_index = tokens.index("--mode")
    if mode_index + 1 >= len(tokens) or tokens[mode_index + 1] != "codex":
        return "Codex is prohibited from invoking user-mode Electron builds."
    return None


def main() -> int:
    """Read one Codex hook event and deny prohibited Bash commands."""
    try:
        event = json.load(sys.stdin)
        command = event.get("tool_input", {}).get("command", "")
    except (AttributeError, json.JSONDecodeError):
        print(
            "Invalid PreToolUse input; native-build policy failed closed.",
            file=sys.stderr,
        )
        return 2
    reason = denial_reason(command)
    if reason is None:
        return 0
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
