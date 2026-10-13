#!/usr/bin/python3
"""Immutable backup and object-preservation support for metadata recovery."""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
from pathlib import Path
import re
import secrets
import shutil
from typing import List, Optional, Tuple

from build_safety import BuildSafetyError


@dataclasses.dataclass(frozen=True)
class MetadataBackup:
    """Location and recorded files for one immutable metadata backup."""

    path: Path
    files: tuple


@dataclasses.dataclass(frozen=True)
class ObjectInventory:
    """Stable identity of compiled object files under one output."""

    count: int
    total_size: int
    digest: str


CORE_METADATA = (
    "args.gn",
    "build.ninja",
    "build.ninja.d",
    "build.ninja.stamp",
    "toolchain.ninja",
    ".ninja_log",
    ".ninja_deps",
)


def create_metadata_backup(
    output: Path, backup_root: Path, identifier: Optional[str] = None
) -> MetadataBackup:
    """Copy narrowly scoped Ninja/GN metadata outside the active output."""
    output = output.resolve()
    backup_root = backup_root.resolve()
    if _is_within(backup_root, output):
        raise BuildSafetyError("metadata backup root must be outside the active output")
    token = identifier or "{}-{}".format(
        datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        secrets.token_hex(6),
    )
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", token):
        raise BuildSafetyError("invalid metadata backup identifier")
    destination = backup_root / "{}-{}".format(output.name, token)
    try:
        destination.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise BuildSafetyError(
            "metadata backup already exists: {}".format(destination)
        ) from error

    relative_files = _metadata_files(output)
    records = []
    for relative in relative_files:
        source = output / relative
        if not source.is_file():
            raise BuildSafetyError(
                "required metadata file is missing: {}; partial backup remains at {}".format(
                    source, destination
                )
            )
        copied = destination / "files" / relative
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, copied)
        source_hash = _sha256(source)
        copied_hash = _sha256(copied)
        if source_hash != copied_hash or source.stat().st_size != copied.stat().st_size:
            raise BuildSafetyError(
                "metadata backup verification failed for {}; partial backup remains at {}".format(
                    relative, destination
                )
            )
        stat = source.stat()
        records.append(
            {
                "path": relative.as_posix(),
                "size": stat.st_size,
                "mode": stat.st_mode,
                "mtime_ns": stat.st_mtime_ns,
                "sha256": source_hash,
            }
        )
    manifest = {
        "source_output": str(output),
        "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "files": records,
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return MetadataBackup(path=destination, files=tuple(relative_files))


def compiled_object_inventory(output: Path) -> ObjectInventory:
    """Fingerprint object files without reading their potentially large contents."""
    root = output / "obj"
    entries: List[Tuple[str, int, int]] = []
    if root.is_dir():
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in (".o", ".obj", ".rlib"):
                stat = path.stat()
                entries.append(
                    (
                        path.relative_to(output).as_posix(),
                        stat.st_size,
                        stat.st_mtime_ns,
                    )
                )
    entries.sort()
    digest = hashlib.sha256()
    for path, size, modified in entries:
        digest.update("{}\0{}\0{}\n".format(path, size, modified).encode("utf-8"))
    return ObjectInventory(
        count=len(entries),
        total_size=sum(size for _path, size, _modified in entries),
        digest=digest.hexdigest(),
    )


def _metadata_files(output: Path) -> List[Path]:
    files = [Path(name) for name in CORE_METADATA]
    log = Path("gn_logs.txt")
    if (output / log).is_file():
        files.append(log)
    manifest = output / "build.ninja"
    if manifest.is_file():
        text = manifest.read_text(encoding="utf-8", errors="replace")
        for value in re.findall(r"^subninja\s+(.+?)\s*$", text, re.MULTILINE):
            relative = Path(value.replace("$ ", " "))
            if relative.is_absolute() or ".." in relative.parts:
                raise BuildSafetyError(
                    "unsafe subninja path in build.ninja: {}".format(value)
                )
            if relative not in files:
                files.append(relative)
    return files


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
        return True
    except ValueError:
        return False
