#!/usr/bin/python3
"""Safety primitives for user-operated Ninja metadata recovery."""

from __future__ import annotations

import dataclasses
import datetime
from pathlib import Path
import re
import shlex
from typing import Optional

from build_safety import BuildSafetyError


EXPECTED_GENERATOR_COMMAND = (
    "../../buildtools/mac/gn --root=../.. -q --regeneration gen ."
)


@dataclasses.dataclass(frozen=True)
class GeneratorEdge:
    """Validated identity of the existing Ninja generator edge."""

    target: str
    depfile: str
    command: str


def validate_recovery_generator(output: Path) -> GeneratorEdge:
    """Require the exact isolated GN generator edge expected by recovery."""
    manifest = output / "build.ninja"
    if not manifest.is_file():
        raise BuildSafetyError(
            "generator edge cannot be validated; build.ninja is missing"
        )
    text = manifest.read_text(encoding="utf-8", errors="replace")
    rule = re.search(r"^rule gn\n(?P<body>(?:  .*\n)+)", text, re.MULTILINE)
    if rule is None:
        raise BuildSafetyError("expected GN generator edge is missing")
    command = _binding(rule.group("body"), "command")
    edges = re.findall(r"^build ([^:\n]+): gn\s*$", text, re.MULTILINE)
    edge = re.search(
        r"^build build\.ninja\.stamp: gn\n(?P<body>(?:  .*\n)+)",
        text,
        re.MULTILINE,
    )
    phony = re.search(
        r"^build build\.ninja: phony build\.ninja\.stamp\n"
        r"(?P<body>(?:  .*\n)+)",
        text,
        re.MULTILINE,
    )
    if (
        command != EXPECTED_GENERATOR_COMMAND
        or edges != ["build.ninja.stamp"]
        or edge is None
        or _binding(edge.group("body"), "generator") != "1"
        or _binding(edge.group("body"), "depfile") != "build.ninja.d"
        or phony is None
        or _binding(phony.group("body"), "generator") != "1"
    ):
        raise BuildSafetyError(
            "existing Ninja generator edge does not match the approved GN recovery edge"
        )
    return GeneratorEdge(
        target="build.ninja.stamp",
        depfile="build.ninja.d",
        command=command,
    )


def validate_recovery_plan(output: str) -> None:
    """Require a dry-run containing exactly one manifest-generator action."""
    actions = []
    for line in output.splitlines():
        match = re.match(r"^\[\d+/(\d+)\]\s+(.+)$", line.strip())
        if match:
            actions.append((int(match.group(1)), match.group(2).strip()))
    if actions != [(1, "Regenerating ninja files")]:
        rendered = " | ".join(description for _total, description in actions)
        raise BuildSafetyError(
            "metadata recovery requires exactly one GN generator action; found {}".format(
                rendered or "no generator action"
            )
        )


def regeneration_diagnostics(output: Path) -> str:
    """Explain the concrete depfile state behind a generator dry-run action."""
    edge = validate_recovery_generator(output)
    depfile = output / edge.depfile
    stamp = output / edge.target
    if not depfile.is_file() or not stamp.is_file():
        raise BuildSafetyError(
            "cannot diagnose regeneration without {} and {}".format(depfile, stamp)
        )
    contents = depfile.read_text(encoding="utf-8", errors="replace")
    if ":" not in contents:
        raise BuildSafetyError("invalid Ninja generator depfile: {}".format(depfile))
    _target, dependencies = contents.split(":", maxsplit=1)
    try:
        values = shlex.split(dependencies.replace("\\\n", " "))
    except ValueError as error:
        raise BuildSafetyError(
            "could not parse Ninja generator depfile: {}".format(depfile)
        ) from error
    stamp_mtime = stamp.stat().st_mtime_ns
    newer = []
    missing = []
    for value in values:
        dependency = output / value
        label = _display_dependency(output, dependency)
        if not dependency.exists():
            missing.append(label)
        elif dependency.stat().st_mtime_ns > stamp_mtime:
            newer.append((label, dependency.stat().st_mtime_ns))

    lines = [
        "Generator edge: {} via {}".format(edge.target, edge.command),
        "Generator depfile: {}".format(edge.depfile),
        "Recorded dependencies: {}".format(len(values)),
    ]
    for label, modified in newer:
        lines.append("newer dependency: {} ({})".format(label, _timestamp(modified)))
    for label in missing:
        lines.append("missing dependency: {}".format(label))
    args = output / "args.gn"
    if args.is_file() and args.stat().st_mtime_ns > stamp_mtime:
        lines.append("args.gn is newer than build.ninja.stamp")
    else:
        lines.append("args.gn is not newer than build.ninja.stamp")

    evaluated = _integer_setting(output / "gn_logs.txt", r"concurrent_links=(\d+)")
    generated = _integer_setting(
        output / "build.ninja",
        r"pool build_toolchain_link_pool\s+depth = (\d+)",
    )
    if evaluated is not None and generated is not None:
        lines.append(
            "generated configuration: concurrent_links={}; link-pool depth={}".format(
                evaluated, generated
            )
        )
        if evaluated != generated:
            lines.append("classification: genuine generated-configuration difference")
        else:
            lines.append(
                "classification: timestamp-driven; semantic currency is not proven"
            )
    else:
        lines.append(
            "classification: dependency-driven; semantic currency could not be compared"
        )
    return "\n".join(lines)


def _display_dependency(output: Path, dependency: Path) -> str:
    try:
        value = dependency.resolve().relative_to(output.resolve())
        return value.as_posix()
    except ValueError:
        return str(dependency.resolve())


def _timestamp(value: int) -> str:
    instant = datetime.datetime.fromtimestamp(
        value / 1_000_000_000, tz=datetime.timezone.utc
    )
    return instant.isoformat()


def _integer_setting(path: Path, pattern: str) -> Optional[int]:
    if not path.is_file():
        return None
    match = re.search(pattern, path.read_text(encoding="utf-8", errors="replace"))
    return int(match.group(1)) if match else None


def _binding(body: str, name: str) -> str:
    """Return one indented Ninja binding from a rule or edge body."""
    match = re.search(r"^  {} = (.*)$".format(re.escape(name)), body, re.MULTILINE)
    return match.group(1).strip() if match else ""
