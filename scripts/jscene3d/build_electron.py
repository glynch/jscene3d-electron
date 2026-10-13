#!/usr/bin/python3
"""Guarded entry point for JScene3D Electron native builds."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
from typing import Dict, Iterator, Mapping, Optional, Sequence, Tuple

from build_metadata_backup import (
    compiled_object_inventory,
    create_metadata_backup,
    MetadataBackup,
)
from build_metadata_recovery import (
    regeneration_diagnostics,
    validate_recovery_generator,
    validate_recovery_plan,
)
from build_safety import BuildSafetyError


EXPECTED_NINJA_LOG_VERSION = 6
EXPECTED_NINJA_DEPS_VERSION = 4
MINIMUM_FREE_BYTES = 50 * 1024 * 1024 * 1024
CODEX_MAX_ACTIONS = 500
CODEX_MAX_NATIVE_ACTIONS = 100
CODEX_MAX_JOBS = 4
CODEX_MAX_LOAD = 4.0
EXPECTED_GN_ARGS = {
    'import("//electron/build/args/testing.gn")',
    'target_cpu = "arm64"',
}
NATIVE_ACTION_PREFIXES = (
    "CC ",
    "CXX ",
    "OBJC ",
    "OBJCXX ",
    "ASM ",
    "RUST ",
    "RUSTC ",
)
GENERATOR_MARKERS = (
    "Regenerating ninja files",
    "build.ninja.stamp",
    " --regeneration gen ",
)


@dataclasses.dataclass(frozen=True)
class Layout:
    """Resolved repository and Chromium checkout paths."""

    electron_root: Path
    source_root: Path
    output_root: Path
    depot_tools: Path
    ninja: Path
    gn: Path
    clang: Path
    rustc: Path


@dataclasses.dataclass(frozen=True)
class Toolchain:
    """Validated host tools used by generated build actions."""

    python: Path
    node: Path
    npm: Path
    developer_dir: Path
    sdk_path: Path
    versions: Mapping[str, str]


@dataclasses.dataclass(frozen=True)
class PythonContract:
    """Checkout-owned Python runtime identity required by build actions."""

    version: Tuple[int, int]
    package_pin: str


@dataclasses.dataclass(frozen=True)
class BuildPlan:
    """Action counts obtained from a pinned-Ninja dry run."""

    total_actions: int
    native_actions: int
    no_work: bool


@dataclasses.dataclass(frozen=True)
class MetadataRecoveryResult:
    """Outcome of metadata-only recovery and its subsequent read-only plan."""

    backup: MetadataBackup
    plan: BuildPlan
    changed_metadata: Tuple[str, ...]


def resolve_layout(script_path: Path) -> Layout:
    """Resolve checkout paths without depending on the working directory."""
    electron_root = script_path.resolve().parents[2]
    source_root = electron_root.parent
    return Layout(
        electron_root=electron_root,
        source_root=source_root,
        output_root=source_root / "out",
        depot_tools=source_root / "third_party" / "depot_tools",
        ninja=source_root / "third_party" / "ninja" / "ninja",
        gn=source_root / "buildtools" / "mac" / "gn",
        clang=source_root
        / "third_party"
        / "llvm-build"
        / "Release+Asserts"
        / "bin"
        / "clang",
        rustc=source_root / "third_party" / "rust-toolchain" / "bin" / "rustc",
    )


def resolve_output(layout: Layout, value: str) -> Path:
    """Resolve an output name or path and keep it inside the Chromium out tree."""
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = layout.output_root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(layout.output_root.resolve())
    except ValueError as error:
        raise BuildSafetyError(
            "output must be inside {}: {}".format(layout.output_root, resolved)
        ) from error
    if resolved == layout.output_root.resolve():
        raise BuildSafetyError("an individual output directory is required")
    return resolved


def require_file(path: Path, description: str, executable: bool = False) -> None:
    """Require one regular file and optionally executable permission."""
    if not path.is_file():
        raise BuildSafetyError("{} is missing: {}".format(description, path))
    if executable and not os.access(str(path), os.X_OK):
        raise BuildSafetyError("{} is not executable: {}".format(description, path))


def command_output(
    command: Sequence[str],
    *,
    environment: Optional[Mapping[str, str]] = None,
    cwd: Optional[Path] = None,
) -> str:
    """Run a read-only tool-identification command and return trimmed output."""
    try:
        result = subprocess.run(
            list(command),
            cwd=str(cwd) if cwd else None,
            env=dict(environment) if environment else None,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stdout", None) or str(error)
        raise BuildSafetyError(
            "command failed: {}\n{}".format(" ".join(command), detail.strip())
        ) from error
    return result.stdout.strip()


def sha256(path: Path) -> str:
    """Return the SHA-256 identity of a tool executable."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def last_output_line(value: str, description: str) -> str:
    """Return the last non-empty line after tools emit optional diagnostics."""
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines:
        raise BuildSafetyError("{} produced no output".format(description))
    return lines[-1]


def chromium_pins(source_root: Path) -> Tuple[str, str]:
    """Read expected Ninja and GN identities from Chromium DEPS."""
    deps = (source_root / "DEPS").read_text(encoding="utf-8")
    ninja_match = re.search(r"'ninja_version':\s*'version:\d+@([0-9.]+)", deps)
    gn_match = re.search(r"'gn_version':\s*'git_revision:([0-9a-f]+)'", deps)
    if not ninja_match or not gn_match:
        raise BuildSafetyError("could not read Ninja/GN pins from Chromium DEPS")
    ninja_package = ninja_match.group(1)
    ninja_version_match = re.match(r"(\d+\.\d+\.\d+)", ninja_package)
    if not ninja_version_match:
        raise BuildSafetyError(
            "unrecognized Ninja package pin: {}".format(ninja_package)
        )
    return ninja_version_match.group(1), gn_match.group(1)


def python_version_pin(path: Path) -> Tuple[int, int]:
    """Read the exact major/minor Python contract from a vpython spec."""
    require_file(path, "Python contract")
    match = re.search(
        r'^python_version:\s*"(\d+)\.(\d+)"\s*$',
        path.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if not match:
        raise BuildSafetyError("could not read Python version from {}".format(path))
    return int(match.group(1)), int(match.group(2))


def read_python_contract(layout: Layout) -> PythonContract:
    """Derive one Python identity from Chromium and depot_tools contracts."""
    chromium_version = python_version_pin(layout.source_root / ".vpython3")
    depot_version = python_version_pin(layout.depot_tools / ".vpython3")
    manifest_name = (
        "manifest_bleeding_edge.txt"
        if (layout.depot_tools / ".bleeding_edge").exists()
        else "manifest.txt"
    )
    manifest = layout.depot_tools / "bootstrap" / manifest_name
    require_file(manifest, "depot_tools bootstrap manifest")
    matches = re.findall(
        r"^infra/3pp/tools/cpython3/\$\{platform\}\s+version:([^\s]+)\s*$",
        manifest.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if len(matches) != 1:
        raise BuildSafetyError(
            "expected one depot_tools CPython package pin in {}".format(manifest)
        )
    package_pin = matches[0]
    package_version = re.search(r"@(\d+)\.(\d+)\.", package_pin)
    if not package_version:
        raise BuildSafetyError(
            "unrecognized depot_tools CPython package pin: {}".format(package_pin)
        )
    manifest_version = (
        int(package_version.group(1)),
        int(package_version.group(2)),
    )
    versions = {
        str(layout.source_root / ".vpython3"): chromium_version,
        str(layout.depot_tools / ".vpython3"): depot_version,
        str(manifest): manifest_version,
    }
    if len(set(versions.values())) != 1:
        rendered = ", ".join(
            "{}={}.{}".format(path, *version) for path, version in versions.items()
        )
        raise BuildSafetyError(
            "checkout Python contracts disagree: {}".format(rendered)
        )
    return PythonContract(
        version=chromium_version,
        package_pin=package_pin,
    )


def selected_managed_python(layout: Layout, contract: PythonContract) -> Optional[Path]:
    """Resolve depot_tools' pointer only when it selects the pinned package."""
    pointer = layout.depot_tools / "python3_bin_reldir.txt"
    if not pointer.is_file():
        return None
    relative_bin = Path(pointer.read_text(encoding="utf-8").strip())
    if relative_bin.is_absolute() or ".." in relative_bin.parts:
        raise BuildSafetyError(
            "depot_tools Python pointer must remain relative: {}".format(pointer)
        )
    expected_root = "bootstrap-{}_bin".format(contract.package_pin)
    if not relative_bin.parts or relative_bin.parts[0] != expected_root:
        return None
    executable = layout.depot_tools / relative_bin / "python3"
    if not executable.is_file():
        return None
    require_file(executable, "checkout-managed Python", executable=True)
    return executable.resolve()


def ensure_managed_python(layout: Layout, contract: PythonContract) -> Path:
    """Bootstrap the checkout-pinned Python when its local install is absent."""
    selected = selected_managed_python(layout, contract)
    if selected is not None:
        return selected

    bootstrap = layout.depot_tools / "bootstrap_python3"
    require_file(bootstrap, "depot_tools Python bootstrap")
    print(
        "Bootstrapping checkout-managed Python {} ({})...".format(
            "{}.{}".format(*contract.version), contract.package_pin
        ),
        flush=True,
    )
    bootstrap_environment = {
        key: os.environ[key]
        for key in ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR")
        if key in os.environ
    }
    bootstrap_environment.update(
        {
            "PATH": os.pathsep.join(
                (
                    str(layout.depot_tools / "python-bin"),
                    str(layout.depot_tools),
                    "/usr/bin",
                    "/bin",
                    "/usr/sbin",
                    "/sbin",
                )
            ),
            "DEPOT_TOOLS_DIR": str(layout.depot_tools),
            "DEPOT_TOOLS_UPDATE": "0",
        }
    )
    try:
        command_output(
            (
                "/bin/bash",
                "-c",
                'set -e; source "$1"; bootstrap_python3',
                "jscene3d-python-bootstrap",
                str(bootstrap),
            ),
            environment=bootstrap_environment,
            cwd=layout.depot_tools,
        )
    except BuildSafetyError as error:
        raise BuildSafetyError(
            "checkout-managed Python is unavailable and depot_tools bootstrap "
            "failed; no build was started:\n{}".format(error)
        ) from error

    selected = selected_managed_python(layout, contract)
    if selected is None:
        raise BuildSafetyError(
            "depot_tools bootstrap completed without publishing its pinned "
            "Python pointer or executable"
        )
    return selected


def validate_generated_python_commands(output: Path) -> int:
    """Require generated Python commands to resolve through the PATH shim."""
    executable_pattern = re.compile(
        r"^(?:[A-Za-z_][A-Za-z0-9_]*=[^\s]+\s+)*"
        r'(?:"(?P<quoted>[^"]+)"|(?P<unquoted>[^\s]+))'
    )
    command_count = 0
    for name in ("build.ninja", "toolchain.ninja"):
        path = output / name
        for number, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
        ):
            if not line.lstrip().startswith("command = "):
                continue
            command = line.split("command = ", maxsplit=1)[1]
            match = executable_pattern.match(command)
            if not match:
                continue
            executable = match.group("quoted") or match.group("unquoted")
            if not re.fullmatch(r"(?:.*/)?v?python(?:3(?:\.\d+)*)?", executable):
                continue
            command_count += 1
            if executable != "python3":
                raise BuildSafetyError(
                    "generated build uses unsupported Python executable {!r} "
                    "at {}:{}; build actions must resolve checkout-managed "
                    "python3 through the guarded PATH".format(executable, path, number)
                )
    return command_count


def validate_python_runtime(
    layout: Layout,
    contract: PythonContract,
    selected_python: Path,
    environment: Mapping[str, str],
) -> str:
    """Execute checkout code with the exact Python exposed to Ninja actions."""
    identity = command_output(
        (
            "python3",
            "-c",
            "import os, sys; "
            "print('.'.join(map(str, sys.version_info[:3]))); "
            "print(os.path.realpath(sys.executable))",
        ),
        environment=environment,
        cwd=layout.source_root,
    ).splitlines()
    if len(identity) != 2:
        raise BuildSafetyError("could not identify Python exposed to build actions")
    version = identity[0].strip()
    version_match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not version_match:
        raise BuildSafetyError(
            "unrecognized managed Python version: {}".format(version)
        )
    selected_version = (int(version_match.group(1)), int(version_match.group(2)))
    if selected_version != contract.version:
        raise BuildSafetyError(
            "Python exposed to build actions is {}, but the checkout requires {}.{}".format(
                version, *contract.version
            )
        )
    selected_executable = Path(identity[1].strip()).resolve()
    if selected_executable != selected_python.resolve():
        raise BuildSafetyError(
            "Python exposed to build actions is {}, expected checkout-managed {}".format(
                selected_executable, selected_python.resolve()
            )
        )

    probe = (
        layout.source_root
        / "tools"
        / "metrics"
        / "histograms"
        / "generate_allowlist_from_histograms_file.py"
    )
    require_file(probe, "checkout Python compatibility probe")
    try:
        command_output(
            ("python3", str(probe), "--help"),
            environment=environment,
            cwd=layout.source_root,
        )
    except BuildSafetyError as error:
        raise BuildSafetyError(
            "checkout Python compatibility probe failed with {} ({}); "
            "no build was started:\n{}".format(selected_python, version, error)
        ) from error
    return version


def validate_pinned_tools(layout: Layout) -> Dict[str, str]:
    """Validate repository-pinned native build tools without modifying outputs."""
    for path, description in (
        (layout.ninja, "pinned Ninja"),
        (layout.gn, "pinned GN"),
        (layout.clang, "pinned Clang"),
        (layout.rustc, "pinned Rust compiler"),
    ):
        require_file(path, description, executable=True)

    expected_ninja, expected_gn_revision = chromium_pins(layout.source_root)
    ninja_version = command_output((str(layout.ninja), "--version"))
    if ninja_version != expected_ninja:
        raise BuildSafetyError(
            "pinned Ninja reports {} but DEPS requires {}".format(
                ninja_version, expected_ninja
            )
        )
    gn_version = command_output((str(layout.gn), "--version"))
    if expected_gn_revision[:12] not in gn_version:
        raise BuildSafetyError(
            "pinned GN identity does not match DEPS: {} (expected {})".format(
                gn_version, expected_gn_revision
            )
        )
    return {
        "ninja": ninja_version,
        "ninja_sha256": sha256(layout.ninja),
        "gn": gn_version,
        "gn_sha256": sha256(layout.gn),
        "clang": command_output((str(layout.clang), "--version")).splitlines()[0],
        "rustc": command_output((str(layout.rustc), "--version")).splitlines()[0],
    }


def resolve_node(electron_root: Path) -> Tuple[Path, Path, str, str]:
    """Resolve the installed Node major declared by Electron's .nvmrc."""
    required = (
        (electron_root / ".nvmrc").read_text(encoding="utf-8").strip().lstrip("v")
    )
    required_major = required.split(".", maxsplit=1)[0]
    current_node = shutil.which("node")
    node = None
    if current_node:
        current_version = command_output((current_node, "--version")).lstrip("v")
        if current_version.split(".", maxsplit=1)[0] == required_major:
            node = Path(current_node).resolve()
    if node is None:
        fnm = shutil.which("fnm")
        if not fnm:
            raise BuildSafetyError(
                "Node {} is required by .nvmrc; select it or install it with fnm".format(
                    required
                )
            )
        node_value = command_output(
            (
                fnm,
                "exec",
                "--using",
                required,
                "--",
                "node",
                "-p",
                "process.execPath",
            ),
            cwd=electron_root,
        )
        node = Path(node_value).resolve()
    npm = node.parent / "npm"
    require_file(node, "selected Node", executable=True)
    require_file(npm, "npm beside selected Node", executable=True)
    node_version = command_output((str(node), "--version")).lstrip("v")
    if node_version.split(".", maxsplit=1)[0] != required_major:
        raise BuildSafetyError(
            "selected Node {} does not satisfy .nvmrc major {}".format(
                node_version, required_major
            )
        )
    npm_version = command_output((str(npm), "--version"))
    return node, npm, node_version, npm_version


def validate_apple_toolchain(output: Path) -> Tuple[Path, Path, Dict[str, str]]:
    """Validate Xcode and generated SDK identity used by the existing output."""
    if sys.platform != "darwin":
        raise BuildSafetyError("this JScene3D Electron output requires macOS")
    developer_dir = Path(command_output(("/usr/bin/xcode-select", "-p"))).resolve()
    apple_environment = {
        key: os.environ[key] for key in ("HOME", "TMPDIR") if key in os.environ
    }
    apple_environment.update(
        {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "DEVELOPER_DIR": str(developer_dir),
        }
    )
    sdk_path = Path(
        last_output_line(
            command_output(
                ("/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"),
                environment=apple_environment,
            ),
            "xcrun --show-sdk-path",
        )
    ).resolve()
    toolchain_text = (output / "toolchain.ninja").read_text(
        encoding="utf-8", errors="replace"
    )
    generated_sdks = set(
        re.findall(
            r"/Applications/[^\s]+\.app/Contents/Developer/Platforms/"
            r"MacOSX\.platform/Developer/SDKs/MacOSX[^\s]+\.sdk",
            toolchain_text,
        )
    )
    resolved_generated_sdks = {
        str(Path(generated_sdk).resolve()) for generated_sdk in generated_sdks
    }
    if resolved_generated_sdks and str(sdk_path) not in resolved_generated_sdks:
        raise BuildSafetyError(
            "selected macOS SDK differs from generated output: selected {}; generated {}".format(
                sdk_path, ", ".join(sorted(generated_sdks))
            )
        )
    versions = {
        "xcode": command_output(
            ("/usr/bin/xcodebuild", "-version"), environment=apple_environment
        )
    }
    for tool in ("metal", "metallib", "dtrace", "ibtool"):
        versions[tool] = last_output_line(
            command_output(
                ("/usr/bin/xcrun", "--find", tool),
                environment=apple_environment,
            ),
            "xcrun --find {}".format(tool),
        )
    return developer_dir, sdk_path, versions


def validate_output_metadata(output: Path) -> None:
    """Reject incomplete or incompatible generated build metadata."""
    required = (
        "args.gn",
        "build.ninja",
        "build.ninja.d",
        "build.ninja.stamp",
        "toolchain.ninja",
        ".ninja_log",
        ".ninja_deps",
    )
    if not output.is_dir():
        raise BuildSafetyError(
            "configured output directory is missing: {}".format(output)
        )
    for name in required:
        require_file(output / name, "generated build metadata")
    if (output / ".siso_deps").exists():
        raise BuildSafetyError("Siso metadata found in Ninja output: {}".format(output))

    with (output / ".ninja_log").open(
        "r", encoding="utf-8", errors="replace"
    ) as stream:
        log_header = stream.readline().strip()
    expected_header = "# ninja log v{}".format(EXPECTED_NINJA_LOG_VERSION)
    if log_header != expected_header:
        raise BuildSafetyError(
            "incompatible Ninja log; expected {!r}, found {!r}. "
            "Do not run another Ninja version against this output.".format(
                expected_header, log_header
            )
        )

    with (output / ".ninja_deps").open("rb") as stream:
        deps_header = stream.read(16)
    if len(deps_header) < 16 or not deps_header.startswith(b"# ninjadeps\n"):
        raise BuildSafetyError("invalid .ninja_deps header in {}".format(output))
    deps_version = struct.unpack("<I", deps_header[12:16])[0]
    if deps_version != EXPECTED_NINJA_DEPS_VERSION:
        raise BuildSafetyError(
            "incompatible .ninja_deps version {}; expected {}".format(
                deps_version, EXPECTED_NINJA_DEPS_VERSION
            )
        )

    arguments = {
        line.strip()
        for line in (output / "args.gn").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    if arguments != EXPECTED_GN_ARGS:
        raise BuildSafetyError(
            "unexpected GN configuration in {}: expected {}; found {}".format(
                output / "args.gn", sorted(EXPECTED_GN_ARGS), sorted(arguments)
            )
        )
    build_ninja = (output / "build.ninja").read_text(encoding="utf-8", errors="replace")
    if "../../buildtools/mac/gn" not in build_ninja:
        raise BuildSafetyError("build.ninja does not reference the pinned macOS GN")
    validate_generated_python_commands(output)


def validate_disk_space(output: Path) -> None:
    """Require enough free space to continue a Chromium build safely."""
    free = shutil.disk_usage(str(output)).free
    if free < MINIMUM_FREE_BYTES:
        raise BuildSafetyError(
            "only {:.1f} GiB is free; at least {:.0f} GiB is required".format(
                free / (1024**3), MINIMUM_FREE_BYTES / (1024**3)
            )
        )


def detect_running_build(output: Path, strict_process_scan: bool) -> None:
    """Reject a concurrent build using the same output where observable."""
    lsof = subprocess.run(
        (
            "/usr/sbin/lsof",
            str(output / ".ninja_log"),
            str(output / ".ninja_deps"),
        ),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if lsof.returncode not in (0, 1):
        raise BuildSafetyError(
            "could not inspect Ninja metadata ownership:\n{}".format(
                lsof.stdout.strip()
            )
        )
    open_files = [line for line in lsof.stdout.splitlines() if line.strip()][1:]
    if open_files:
        raise BuildSafetyError(
            "a process has Ninja metadata open for this output:\n{}".format(
                "\n".join(open_files)
            )
        )
    try:
        listing = command_output(("/bin/ps", "-axo", "pid=,command="))
    except BuildSafetyError:
        if strict_process_scan:
            raise
        print(
            "Warning: process-table inspection is unavailable; "
            "the guarded output lock and Ninja metadata ownership check passed.",
            file=sys.stderr,
        )
        return
    output_values = (str(output), output.name)
    conflicts = []
    for line in listing.splitlines():
        if not any(value in line for value in output_values):
            continue
        if re.search(r"(?:^|[/\s])(?:auto)?ninja(?:\.py)?(?:\s|$)", line):
            conflicts.append(line.strip())
    if conflicts:
        raise BuildSafetyError(
            "another native build is using this output:\n{}".format(
                "\n".join(conflicts)
            )
        )


def validate_electron_dependencies(electron_root: Path) -> None:
    """Require already-installed host dependencies without installing anything."""
    for name in ("webpack", "tsc"):
        require_file(
            electron_root / "node_modules" / ".bin" / name,
            "Electron node_modules tool {}".format(name),
            executable=True,
        )


def validate_host_tools(layout: Layout, output: Path) -> Toolchain:
    """Validate host tools used indirectly by generated actions."""
    python_contract = read_python_contract(layout)
    python = ensure_managed_python(layout, python_contract)
    node, npm, node_version, npm_version = resolve_node(layout.electron_root)
    developer_dir, sdk_path, versions = validate_apple_toolchain(output)
    validate_electron_dependencies(layout.electron_root)
    versions.update(
        {
            "python": "{} ({})".format(
                command_output((str(python), "--version")),
                python_contract.package_pin,
            ),
            "node": node_version,
            "npm": npm_version,
        }
    )
    return Toolchain(
        python=python,
        node=node,
        npm=npm,
        developer_dir=developer_dir,
        sdk_path=sdk_path,
        versions=versions,
    )


@contextlib.contextmanager
def build_environment(layout: Layout, toolchain: Toolchain) -> Iterator[Dict[str, str]]:
    """Yield a deterministic child environment with no Homebrew fallback."""
    with tempfile.TemporaryDirectory(prefix="jscene3d-electron-build-") as temp_value:
        shim = Path(temp_value)
        (shim / "python3").symlink_to(toolchain.python)
        (shim / "python").symlink_to(toolchain.python)
        (shim / "gn").symlink_to(layout.gn)
        (shim / "ninja").symlink_to(layout.ninja)
        preserved = (
            "HOME",
            "USER",
            "LOGNAME",
            "LANG",
            "LC_ALL",
            "TERM",
            "TMPDIR",
        )
        environment = {key: os.environ[key] for key in preserved if key in os.environ}
        environment.update(
            {
                "PATH": os.pathsep.join(
                    (
                        str(shim),
                        str(toolchain.node.parent),
                        str(layout.depot_tools),
                        "/usr/bin",
                        "/bin",
                        "/usr/sbin",
                        "/sbin",
                    )
                ),
                "CHROMIUM_BUILDTOOLS_PATH": str(layout.source_root / "buildtools"),
                "DEVELOPER_DIR": str(toolchain.developer_dir),
                "PYTHONDONTWRITEBYTECODE": "1",
                "NINJA_BUILD_IN_BACKGROUND": "1",
            }
        )
        yield environment


def parse_plan(output: str) -> BuildPlan:
    """Classify a pinned-Ninja dry-run plan and reject generator actions."""
    generator_lines = [
        line.strip()
        for line in output.splitlines()
        if any(marker in line for marker in GENERATOR_MARKERS)
    ]
    if generator_lines:
        raise BuildSafetyError(
            "Ninja wants to regenerate build metadata; explicit review is required: {}".format(
                " | ".join(generator_lines[:3])
            )
        )
    if "ninja: no work to do." in output:
        return BuildPlan(total_actions=0, native_actions=0, no_work=True)
    actions = []
    for line in output.splitlines():
        match = re.match(r"^\[\d+/(\d+)\]\s+(.+)$", line.strip())
        if match:
            actions.append((int(match.group(1)), match.group(2)))
    if not actions:
        raise BuildSafetyError("could not classify the Ninja dry-run plan")
    totals = {total for total, _description in actions}
    if len(totals) != 1:
        raise BuildSafetyError("Ninja dry-run reported inconsistent action totals")
    total_actions = totals.pop()
    if len(actions) != total_actions:
        raise BuildSafetyError(
            "Ninja dry-run output was incomplete: expected {}, classified {}".format(
                total_actions, len(actions)
            )
        )
    native_actions = sum(
        1
        for _total, description in actions
        if description.startswith(NATIVE_ACTION_PREFIXES)
    )
    return BuildPlan(
        total_actions=total_actions,
        native_actions=native_actions,
        no_work=False,
    )


def enforce_mode(mode: str, plan: BuildPlan, jobs: int, load_average: float) -> None:
    """Enforce immutable Codex build limits."""
    if mode != "codex":
        return
    violations = []
    if plan.total_actions > CODEX_MAX_ACTIONS:
        violations.append(
            "{} total actions exceeds {}".format(plan.total_actions, CODEX_MAX_ACTIONS)
        )
    if plan.native_actions > CODEX_MAX_NATIVE_ACTIONS:
        violations.append(
            "{} native actions exceeds {}".format(
                plan.native_actions, CODEX_MAX_NATIVE_ACTIONS
            )
        )
    if jobs > CODEX_MAX_JOBS:
        violations.append("{} jobs exceeds {}".format(jobs, CODEX_MAX_JOBS))
    if load_average > CODEX_MAX_LOAD:
        violations.append(
            "load average {} exceeds {}".format(load_average, CODEX_MAX_LOAD)
        )
    if violations:
        raise BuildSafetyError(
            "Codex build refused: {}. The user must run the user-mode command.".format(
                "; ".join(violations)
            )
        )


def dry_run_plan(
    layout: Layout,
    output: Path,
    targets: Sequence[str],
    environment: Mapping[str, str],
) -> BuildPlan:
    """Obtain a read-only action plan from the validated pinned Ninja."""
    command = [str(layout.ninja), "-n", "-C", str(output)] + list(targets)
    rendered = command_output(command, environment=environment)
    try:
        return parse_plan(rendered)
    except BuildSafetyError as error:
        if not any(marker in rendered for marker in GENERATOR_MARKERS):
            raise
        try:
            diagnostic = regeneration_diagnostics(output)
        except BuildSafetyError as diagnostic_error:
            diagnostic = "Regeneration diagnostics unavailable: {}".format(
                diagnostic_error
            )
        raise BuildSafetyError("{}\n{}".format(error, diagnostic)) from error


def recover_metadata(
    layout: Layout,
    output: Path,
    environment: Mapping[str, str],
    tool_versions: Mapping[str, str],
    backup_root: Path,
) -> MetadataRecoveryResult:
    """Regenerate only the validated Ninja generator edge in user mode."""
    edge = validate_recovery_generator(output)
    recovery_command = [
        str(layout.ninja),
        "-n",
        "-C",
        str(output),
        edge.target,
    ]
    validate_recovery_plan(command_output(recovery_command, environment=environment))
    original_manifest = sha256(output / "build.ninja")
    original_args = sha256(output / "args.gn")
    original_objects = compiled_object_inventory(output)
    backup = create_metadata_backup(output, backup_root)
    print("Metadata backup: {}".format(backup.path), flush=True)
    validate_recovery_generator(output)
    if sha256(output / "build.ninja") != original_manifest:
        raise BuildSafetyError(
            "build.ninja changed while its metadata backup was being created; "
            "recovery was not started; backup: {}".format(backup.path)
        )
    validate_recovery_plan(command_output(recovery_command, environment=environment))
    command = [str(layout.ninja), "-C", str(output), edge.target]
    print("Executing metadata recovery: {}".format(" ".join(command)), flush=True)
    try:
        command_output(command, environment=environment)
    except BuildSafetyError as error:
        raise BuildSafetyError(
            "metadata regeneration failed; no automatic restoration was attempted; "
            "backup: {}\n{}".format(backup.path, error)
        ) from error

    try:
        validate_output_metadata(output)
        validate_recovery_generator(output)
        if sha256(output / "args.gn") != original_args:
            raise BuildSafetyError("metadata regeneration changed args.gn")
        if compiled_object_inventory(output) != original_objects:
            raise BuildSafetyError(
                "metadata regeneration changed compiled object files"
            )
        current_versions = validate_pinned_tools(layout)
        if dict(current_versions) != dict(tool_versions):
            raise BuildSafetyError(
                "pinned tool identity changed during metadata regeneration"
            )
        plan = dry_run_plan(layout, output, ("electron",), environment)
        changed = []
        for relative in backup.files:
            previous = backup.path / "files" / relative
            current = output / relative
            if not current.is_file() or sha256(previous) != sha256(current):
                changed.append(relative.as_posix())
    except BuildSafetyError as error:
        raise BuildSafetyError(
            "post-regeneration validation failed; no automatic restoration was "
            "attempted; backup: {}\n{}".format(backup.path, error)
        ) from error
    return MetadataRecoveryResult(
        backup=backup,
        plan=plan,
        changed_metadata=tuple(changed),
    )


@contextlib.contextmanager
def output_lock(output: Path) -> Iterator[None]:
    """Prevent two guarded entry points from planning/building one output."""
    identity = hashlib.sha256(str(output).encode("utf-8")).hexdigest()[:20]
    path = Path(tempfile.gettempdir()) / "jscene3d-electron-{}.lock".format(identity)
    with path.open("a+", encoding="utf-8") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BuildSafetyError(
                "another guarded build owns output {}".format(output)
            ) from error
        yield


def run_build(
    layout: Layout,
    output: Path,
    targets: Sequence[str],
    jobs: int,
    load_average: float,
    environment: Mapping[str, str],
) -> int:
    """Execute the already-planned build and report its terminal result."""
    command = [
        str(layout.ninja),
        "-C",
        str(output),
        "-j",
        str(jobs),
        "-l",
        str(load_average),
    ] + list(targets)
    print("Executing: {}".format(" ".join(command)), flush=True)
    try:
        process = subprocess.Popen(
            command,
            env=dict(environment),
            start_new_session=True,
        )
    except OSError as error:
        raise BuildSafetyError(
            "could not start pinned Ninja {}: {}".format(layout.ninja, error)
        ) from error

    def forward(signum: int, _frame: object) -> None:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                pass

    previous = {
        signum: signal.signal(signum, forward)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        return_code = process.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    exit_status = 128 - return_code if return_code < 0 else return_code
    target_label = "Target" if len(targets) == 1 else "Targets"
    target_value = ", ".join(targets)
    if return_code == 0:
        print("\nJScene3D Electron build completed successfully.", flush=True)
        print("{}: {}".format(target_label, target_value), flush=True)
        print("Exit status: 0", flush=True)
    else:
        if return_code < 0:
            detail = "terminated by signal {}".format(-return_code)
        else:
            detail = "Ninja exited with status {}".format(return_code)
        print(
            "\nJScene3D Electron build failed: {}.".format(detail),
            file=sys.stderr,
            flush=True,
        )
        print(
            "{}: {}".format(target_label, target_value),
            file=sys.stderr,
            flush=True,
        )
        print(
            "Exit status: {}".format(exit_status),
            file=sys.stderr,
            flush=True,
        )
    return exit_status


def positive_integer(value: str) -> int:
    """Parse a positive command-line integer."""
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def positive_float(value: str) -> float:
    """Parse a positive command-line float."""
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def parse_arguments(arguments: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse the intentionally narrow build interface."""
    parser = argparse.ArgumentParser(
        description="Safely preflight and build JScene3D Electron"
    )
    parser.add_argument("--mode", choices=("user", "codex"), required=True)
    parser.add_argument("--output", default="JScene3D-Electron42")
    parser.add_argument("--target", action="append", dest="targets", default=[])
    parser.add_argument("--jobs", type=positive_integer)
    parser.add_argument("--load-average", type=positive_float)
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument("--preflight-only", action="store_true")
    execution.add_argument("--build", action="store_true")
    parser.add_argument("--regenerate-only", action="store_true")
    parsed = parser.parse_args(arguments)
    if parsed.regenerate_only:
        if parsed.mode != "user":
            parser.error("--regenerate-only requires --mode user")
        incompatible = []
        if parsed.preflight_only:
            incompatible.append("--preflight-only")
        if parsed.build:
            incompatible.append("--build")
        if parsed.targets:
            incompatible.append("--target")
        if parsed.jobs is not None:
            incompatible.append("--jobs")
        if parsed.load_average is not None:
            incompatible.append("--load-average")
        if incompatible:
            parser.error(
                "--regenerate-only cannot be combined with {}".format(
                    ", ".join(incompatible)
                )
            )
    elif not parsed.build:
        parsed.preflight_only = True
    if not parsed.regenerate_only and not parsed.targets:
        parsed.targets = ["electron"]
    for target in parsed.targets:
        if not re.fullmatch(r"[A-Za-z0-9_./:+-]+", target) or target.startswith("-"):
            parser.error("invalid Ninja target: {}".format(target))
        if target in ("clean", "gn_all"):
            parser.error("prohibited Ninja target: {}".format(target))
    default_jobs = (
        CODEX_MAX_JOBS if parsed.mode == "codex" else min(os.cpu_count() or 4, 8)
    )
    parsed.jobs = parsed.jobs or default_jobs
    parsed.load_average = parsed.load_average or float(parsed.jobs)
    return parsed


def print_identity(
    layout: Layout, output: Path, toolchain: Toolchain, versions: Mapping[str, str]
) -> None:
    """Print enough identity information to diagnose stale environments."""
    print("JScene3D Electron build preflight")
    print("  source: {}".format(layout.source_root))
    print("  output: {}".format(output))
    print("  Ninja: {} ({})".format(layout.ninja, versions["ninja"]))
    print("  Ninja SHA-256: {}".format(versions["ninja_sha256"]))
    print("  GN: {} ({})".format(layout.gn, versions["gn"]))
    print("  GN SHA-256: {}".format(versions["gn_sha256"]))
    for name in ("python", "node", "npm", "clang", "rustc", "xcode"):
        print("  {}: {}".format(name, toolchain.versions.get(name, versions.get(name))))
    print("  SDK: {}".format(toolchain.sdk_path))


def main(arguments: Optional[Sequence[str]] = None) -> int:
    """Run validation, immutable plan limits, and optionally the build."""
    parsed = parse_arguments(arguments)
    layout = resolve_layout(Path(__file__))
    output = resolve_output(layout, parsed.output)
    try:
        with output_lock(output):
            validate_output_metadata(output)
            validate_disk_space(output)
            detect_running_build(output, strict_process_scan=parsed.mode == "user")
            versions = validate_pinned_tools(layout)
            toolchain = validate_host_tools(layout, output)
            print_identity(layout, output, toolchain, versions)
            sys.stdout.flush()
            with build_environment(layout, toolchain) as environment:
                validate_python_runtime(
                    layout,
                    read_python_contract(layout),
                    toolchain.python,
                    environment,
                )
                if parsed.regenerate_only:
                    print(regeneration_diagnostics(output))
                    result = recover_metadata(
                        layout,
                        output,
                        environment,
                        versions,
                        layout.output_root / ".jscene3d-metadata-backups",
                    )
                    try:
                        refreshed_toolchain = validate_host_tools(layout, output)
                    except BuildSafetyError as error:
                        raise BuildSafetyError(
                            "post-regeneration host-tool validation failed; backup: {}\n{}".format(
                                result.backup.path, error
                            )
                        ) from error
                    if refreshed_toolchain != toolchain:
                        raise BuildSafetyError(
                            "selected host toolchain changed during metadata recovery; "
                            "backup: {}".format(result.backup.path)
                        )
                    print(
                        "Metadata changed: {}".format(
                            ", ".join(result.changed_metadata) or "none"
                        )
                    )
                    print(
                        "Post-recovery plan: {} total actions, {} native "
                        "compilation actions".format(
                            result.plan.total_actions, result.plan.native_actions
                        )
                    )
                    print("Metadata recovery completed; no build was started.")
                    return 0
                plan = dry_run_plan(layout, output, parsed.targets, environment)
                enforce_mode(parsed.mode, plan, parsed.jobs, parsed.load_average)
                print(
                    "Plan: {} total actions, {} native compilation actions".format(
                        plan.total_actions, plan.native_actions
                    )
                )
                if parsed.preflight_only or plan.no_work:
                    print("Preflight passed; no build was started.")
                    return 0
                return run_build(
                    layout,
                    output,
                    parsed.targets,
                    parsed.jobs,
                    parsed.load_average,
                    environment,
                )
    except BuildSafetyError as error:
        print("JScene3D Electron build refused: {}".format(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
