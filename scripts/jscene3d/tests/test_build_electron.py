"""Unit tests for the guarded JScene3D Electron build entry point."""

from __future__ import annotations

import contextlib
import errno
import io
import importlib.util
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "build_electron.py"
sys.path.insert(0, str(SCRIPT.parent))
SPEC = importlib.util.spec_from_file_location("build_electron", SCRIPT)
assert SPEC and SPEC.loader
build_electron = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = build_electron
SPEC.loader.exec_module(build_electron)

HOOK_SCRIPT = (
    Path(__file__).resolve().parents[3] / ".codex" / "hooks" / "pre_tool_use_policy.py"
)
HOOK_SPEC = importlib.util.spec_from_file_location("pre_tool_use_policy", HOOK_SCRIPT)
assert HOOK_SPEC and HOOK_SPEC.loader
pre_tool_use_policy = importlib.util.module_from_spec(HOOK_SPEC)
sys.modules[HOOK_SPEC.name] = pre_tool_use_policy
HOOK_SPEC.loader.exec_module(pre_tool_use_policy)


class BuildElectronTest(unittest.TestCase):
    """Exercise build safety without invoking Ninja, GN, or compilation."""

    def test_regeneration_is_an_explicit_user_only_operation(self) -> None:
        parsed = build_electron.parse_arguments(("--mode", "user", "--regenerate-only"))

        self.assertTrue(parsed.regenerate_only)
        self.assertEqual([], parsed.targets)

        incompatible = (
            ("--mode", "codex", "--regenerate-only"),
            ("--mode", "user", "--regenerate-only", "--preflight-only"),
            ("--mode", "user", "--regenerate-only", "--target", "electron"),
            ("--mode", "user", "--regenerate-only", "--jobs", "2"),
            ("--mode", "user", "--regenerate-only", "--load-average", "2"),
            ("--mode", "user", "--regenerate-only", "--build"),
        )
        for arguments in incompatible:
            with self.subTest(arguments=arguments):
                with self.assertRaises(SystemExit):
                    build_electron.parse_arguments(arguments)

    def test_preflight_is_default_and_build_execution_is_explicit(self) -> None:
        preflight = build_electron.parse_arguments(("--mode", "user"))
        self.assertTrue(preflight.preflight_only)
        self.assertFalse(preflight.build)

        build = build_electron.parse_arguments(("--mode", "user", "--build"))
        self.assertFalse(build.preflight_only)
        self.assertTrue(build.build)

        with self.assertRaises(SystemExit):
            build_electron.parse_arguments(
                ("--mode", "user", "--preflight-only", "--build")
            )

    def test_output_must_remain_inside_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            layout = self.layout(root)
            accepted = build_electron.resolve_output(layout, "safe-output")
            self.assertEqual((layout.output_root / "safe-output").resolve(), accepted)
            with self.assertRaises(build_electron.BuildSafetyError):
                build_electron.resolve_output(layout, "../escaped")

    def test_ninja_v6_metadata_and_expected_gn_args_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            output = self.output_fixture(Path(value), log_version=6)
            build_electron.validate_output_metadata(output)

    def test_incompatible_ninja_log_is_rejected_before_ninja_runs(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            output = self.output_fixture(Path(value), log_version=7)
            with self.assertRaisesRegex(
                build_electron.BuildSafetyError, "incompatible Ninja log"
            ):
                build_electron.validate_output_metadata(output)

    def test_unexpected_gn_configuration_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            output = self.output_fixture(Path(value), log_version=6)
            (output / "args.gn").write_text(
                'import("//electron/build/args/testing.gn")\ntarget_cpu = "x64"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                build_electron.BuildSafetyError, "unexpected GN configuration"
            ):
                build_electron.validate_output_metadata(output)

    def test_plan_counts_native_actions(self) -> None:
        plan = build_electron.parse_plan(
            "\n".join(
                (
                    "[1/5] CXX obj/one.o",
                    "[2/5] ACTION generate",
                    "[3/5] OBJCXX obj/two.o",
                    "[4/5] RUST obj/three.rlib",
                    "[5/5] LINK electron",
                )
            )
        )
        self.assertEqual(5, plan.total_actions)
        self.assertEqual(3, plan.native_actions)
        self.assertFalse(plan.no_work)

    def test_last_output_line_ignores_tool_diagnostics(self) -> None:
        self.assertEqual(
            "/Applications/Xcode.app/SDK",
            build_electron.last_output_line(
                "xcrun: warning: temporary directory unavailable\n"
                "/Applications/Xcode.app/SDK\n",
                "xcrun",
            ),
        )

    def test_plan_rejects_implicit_gn_regeneration(self) -> None:
        with self.assertRaisesRegex(
            build_electron.BuildSafetyError,
            r"regenerate build metadata.*\[1/1\] Regenerating ninja files",
        ):
            build_electron.parse_plan("[1/1] Regenerating ninja files")

    def test_plan_rejects_incomplete_output(self) -> None:
        with self.assertRaisesRegex(
            build_electron.BuildSafetyError, "output was incomplete"
        ):
            build_electron.parse_plan("[1/2] CXX obj/one.o")

    def test_successful_build_waits_and_reports_unambiguous_completion(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            layout = self.layout(root)
            process = mock.Mock()
            process.wait.return_value = 0
            output = io.StringIO()

            with (
                mock.patch.object(
                    build_electron.subprocess, "Popen", return_value=process
                ),
                contextlib.redirect_stdout(output),
            ):
                status = build_electron.run_build(
                    layout,
                    root / "out" / "fixture",
                    ("electron",),
                    8,
                    8.0,
                    {"PATH": "/managed"},
                )

            self.assertEqual(0, status)
            process.wait.assert_called_once_with()
            self.assertIn(
                "JScene3D Electron build completed successfully.",
                output.getvalue(),
            )
            self.assertIn("Target: electron", output.getvalue())
            self.assertIn("Exit status: 0", output.getvalue())

    def test_failed_build_reports_and_preserves_nonzero_exit_status(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            layout = self.layout(root)
            process = mock.Mock()
            process.wait.return_value = 7
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                mock.patch.object(
                    build_electron.subprocess, "Popen", return_value=process
                ),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                status = build_electron.run_build(
                    layout,
                    root / "out" / "fixture",
                    ("electron",),
                    8,
                    8.0,
                    {"PATH": "/managed"},
                )

            self.assertEqual(7, status)
            process.wait.assert_called_once_with()
            self.assertNotIn("completed successfully", stdout.getvalue())
            self.assertIn("Ninja exited with status 7", stderr.getvalue())
            self.assertIn("Target: electron", stderr.getvalue())
            self.assertIn("Exit status: 7", stderr.getvalue())

    def test_signal_return_code_maps_to_conventional_shell_status(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            layout = self.layout(root)
            process = mock.Mock()
            process.wait.return_value = -signal.SIGTERM
            stderr = io.StringIO()

            with (
                mock.patch.object(
                    build_electron.subprocess, "Popen", return_value=process
                ),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(stderr),
            ):
                status = build_electron.run_build(
                    layout,
                    root / "out" / "fixture",
                    ("electron",),
                    8,
                    8.0,
                    {"PATH": "/managed"},
                )

            self.assertEqual(128 + signal.SIGTERM, status)
            self.assertIn("terminated by signal 15", stderr.getvalue())
            self.assertIn("Exit status: 143", stderr.getvalue())

    def test_ninja_inherits_output_descriptors_and_owns_a_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            layout = self.layout(root)
            process = mock.Mock()
            process.wait.return_value = 0

            with (
                mock.patch.object(
                    build_electron.subprocess, "Popen", return_value=process
                ) as popen,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                build_electron.run_build(
                    layout,
                    root / "out" / "fixture",
                    ("electron",),
                    8,
                    8.0,
                    {"PATH": "/managed"},
                )

            kwargs = popen.call_args.kwargs
            self.assertNotIn("stdout", kwargs)
            self.assertNotIn("stderr", kwargs)
            self.assertTrue(kwargs["start_new_session"])

    def test_synthetic_build_preserves_streams_carriage_returns_and_buffering(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            command = self.synthetic_build_command(
                root,
                """#!/usr/bin/python3
import os
import sys

sys.stdout.buffer.write(b"[1/2] first\\r[2/2] second\\n")
sys.stdout.buffer.flush()
sys.stderr.buffer.write(b"synthetic stderr\\n")
sys.stderr.buffer.flush()
sys.stdout.write("TTY={}\\n".format(int(os.isatty(1))))
sys.stdout.write("buffered child completion\\n")
""",
            )

            result = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertEqual(0, result.returncode)
            self.assertIn(b"[1/2] first\r[2/2] second\n", result.stdout)
            self.assertIn(b"synthetic stderr\n", result.stderr)
            self.assertIn(b"TTY=0\n", result.stdout)
            child = result.stdout.index(b"buffered child completion")
            wrapper = result.stdout.index(
                b"JScene3D Electron build completed successfully."
            )
            self.assertLess(child, wrapper)

    def test_synthetic_build_preserves_interactive_terminal_detection(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            command = self.synthetic_build_command(
                root,
                """#!/usr/bin/python3
import os

print("TTY={}".format(int(os.isatty(1))), flush=True)
""",
            )
            master, slave = os.openpty()
            try:
                process = subprocess.Popen(command, stdout=slave, stderr=slave)
                os.close(slave)
                slave = -1
                chunks = []
                while True:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError as error:
                        if error.errno == errno.EIO:
                            break
                        raise
                    if not chunk:
                        break
                    chunks.append(chunk)
                status = process.wait(timeout=5)
            finally:
                os.close(master)
                if slave >= 0:
                    os.close(slave)

            output = b"".join(chunks)
            self.assertEqual(0, status)
            self.assertIn(b"TTY=1", output)
            self.assertIn(b"JScene3D Electron build completed successfully.", output)

    @unittest.skipUnless(hasattr(os, "killpg"), "requires POSIX process groups")
    def test_termination_signal_reaches_ninja_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            descendant_pid = root / "descendant.pid"
            missed_group_signal = root / "missed-group-signal"
            ready = root / "ready"
            command = self.synthetic_build_command(
                root,
                """#!/usr/bin/python3
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

descendant = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)"]
)
Path(os.environ["DESCENDANT_PID"]).write_text(str(descendant.pid))

def stop(signum, _frame):
    try:
        descendant.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        Path(os.environ["MISSED_GROUP_SIGNAL"]).write_text("missed")
        descendant.terminate()
        descendant.wait(timeout=2)
    raise SystemExit(128 + signum)

signal.signal(signal.SIGTERM, stop)
Path(os.environ["READY_FILE"]).write_text("ready")
while True:
    time.sleep(1)
""",
                extra_environment={
                    "DESCENDANT_PID": str(descendant_pid),
                    "MISSED_GROUP_SIGNAL": str(missed_group_signal),
                    "READY_FILE": str(ready),
                },
            )
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            try:
                for _attempt in range(500):
                    if ready.is_file():
                        break
                    if process.poll() is not None:
                        self.fail("synthetic build exited before becoming ready")
                    time.sleep(0.01)
                else:
                    self.fail("synthetic build did not become ready")

                process.send_signal(signal.SIGTERM)
                _stdout, stderr = process.communicate(timeout=5)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

            self.assertEqual(128 + signal.SIGTERM, process.returncode)
            self.assertFalse(missed_group_signal.exists())
            self.assertIn(b"Exit status: 143", stderr)

    def test_ninja_launch_failure_is_a_build_safety_error(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            layout = self.layout(root)

            with (
                mock.patch.object(
                    build_electron.subprocess,
                    "Popen",
                    side_effect=OSError("synthetic launch failure"),
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaisesRegex(
                    build_electron.BuildSafetyError,
                    "could not start pinned Ninja.*synthetic launch failure",
                ):
                    build_electron.run_build(
                        layout,
                        root / "out" / "fixture",
                        ("electron",),
                        8,
                        8.0,
                        {"PATH": "/managed"},
                    )

    def test_recovery_requires_the_existing_isolated_generator_edge(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            output = self.output_fixture(Path(value), log_version=6)
            self.write_generator_manifest(output)

            edge = build_electron.validate_recovery_generator(output)

            self.assertEqual("build.ninja.stamp", edge.target)
            self.assertEqual("build.ninja.d", edge.depfile)
            self.assertEqual(
                "../../buildtools/mac/gn --root=../.. -q --regeneration gen .",
                edge.command,
            )

            (output / "build.ninja").write_text(
                "rule gn\n  command = ../../buildtools/mac/gn gen .\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                build_electron.BuildSafetyError, "generator edge"
            ):
                build_electron.validate_recovery_generator(output)

    def test_recovery_plan_allows_only_one_generator_action(self) -> None:
        build_electron.validate_recovery_plan(
            "ninja: Entering directory `/fixture'\n[0/1] Regenerating ninja files\n"
        )

        invalid_plans = (
            "ninja: no work to do.\n",
            "[0/2] Regenerating ninja files\n[1/2] Regenerating ninja files\n",
            "[0/2] Regenerating ninja files\n[1/2] CXX obj/unsafe.o\n",
            "[0/2] Regenerating ninja files\n[1/2] LINK electron\n",
        )
        for plan in invalid_plans:
            with self.subTest(plan=plan):
                with self.assertRaises(build_electron.BuildSafetyError):
                    build_electron.validate_recovery_plan(plan)

    def test_metadata_backup_is_complete_verified_and_collision_safe(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            output = self.output_fixture(root, log_version=6)
            self.write_generator_manifest(output)
            with (output / "build.ninja").open("a", encoding="utf-8") as stream:
                stream.write("\nsubninja secondary/toolchain.ninja\n")
            secondary = output / "secondary" / "toolchain.ninja"
            secondary.parent.mkdir()
            secondary.write_text("rule secondary\n", encoding="utf-8")
            (output / "gn_logs.txt").write_text(
                "concurrent_links=10\n", encoding="utf-8"
            )
            backup_root = root / "metadata-backups"

            backup = build_electron.create_metadata_backup(
                output, backup_root, identifier="fixed"
            )

            self.assertEqual((backup_root / "fixture-fixed").resolve(), backup.path)
            manifest = json.loads(
                (backup.path / "manifest.json").read_text(encoding="utf-8")
            )
            backed_up = {entry["path"] for entry in manifest["files"]}
            self.assertIn("build.ninja", backed_up)
            self.assertIn(".ninja_log", backed_up)
            self.assertIn(".ninja_deps", backed_up)
            self.assertIn("gn_logs.txt", backed_up)
            self.assertIn("secondary/toolchain.ninja", backed_up)
            for entry in manifest["files"]:
                copied = backup.path / "files" / entry["path"]
                self.assertEqual(entry["size"], copied.stat().st_size)
                self.assertEqual(entry["sha256"], build_electron.sha256(copied))

            with self.assertRaisesRegex(
                build_electron.BuildSafetyError, "backup already exists"
            ):
                build_electron.create_metadata_backup(
                    output, backup_root, identifier="fixed"
                )

    def test_regeneration_diagnostics_identify_actual_stale_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            output = self.output_fixture(Path(value), log_version=6)
            self.write_generator_manifest(output)
            with (output / "build.ninja").open("a", encoding="utf-8") as stream:
                stream.write("\npool build_toolchain_link_pool\n  depth = 1\n")
            stamp = output / "build.ninja.stamp"
            args = output / "args.gn"
            log = output / "gn_logs.txt"
            log.write_text("concurrent_links=10\n", encoding="utf-8")
            (output / "build.ninja.d").write_text(
                "build.ninja.stamp: ./args.gn ./gn_logs.txt ./missing.gni\n",
                encoding="utf-8",
            )
            os.utime(args, ns=(1_000_000_000, 1_000_000_000))
            os.utime(stamp, ns=(2_000_000_000, 2_000_000_000))
            os.utime(log, ns=(3_000_000_000, 3_000_000_000))

            diagnostic = build_electron.regeneration_diagnostics(output)

            self.assertIn("newer dependency: gn_logs.txt", diagnostic)
            self.assertIn("missing dependency: missing.gni", diagnostic)
            self.assertIn("args.gn is not newer than build.ninja.stamp", diagnostic)
            self.assertIn("concurrent_links=10", diagnostic)
            self.assertIn("link-pool depth=1", diagnostic)
            self.assertIn("genuine generated-configuration difference", diagnostic)

    def test_recovery_executes_only_pinned_generator_and_reports_post_plan(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            layout = self.layout(root)
            output = self.output_fixture(root, log_version=6)
            self.write_generator_manifest(output)
            log = output / "gn_logs.txt"
            log.write_text("concurrent_links=10\n", encoding="utf-8")
            (output / "build.ninja.d").write_text(
                "build.ninja.stamp: ./gn_logs.txt ./args.gn\n", encoding="utf-8"
            )
            object_file = output / "obj" / "preserved.o"
            object_file.parent.mkdir()
            object_file.write_bytes(b"compiled-object")
            before_object = object_file.stat()
            before_args = build_electron.sha256(output / "args.gn")
            commands = []

            def fake_command(command, *, environment=None, cwd=None):
                commands.append(tuple(command))
                if "-n" in command and command[-1] == "build.ninja.stamp":
                    return "[0/1] Regenerating ninja files"
                if command[-1] == "build.ninja.stamp":
                    os.utime(output / "build.ninja.stamp", None)
                    return "[0/1] Regenerating ninja files"
                if "-n" in command and command[-1] == "electron":
                    return "[1/2] ACTION generate\n[2/2] CXX obj/remaining.o"
                self.fail("unexpected recovery command: {}".format(command))

            versions = {"ninja": "1.12.1", "gn_sha256": "gn"}
            with (
                mock.patch.object(
                    build_electron, "command_output", side_effect=fake_command
                ),
                mock.patch.object(
                    build_electron, "validate_pinned_tools", return_value=versions
                ),
            ):
                result = build_electron.recover_metadata(
                    layout,
                    output,
                    {"PATH": "/managed"},
                    versions,
                    root / "metadata-backups",
                )

            self.assertEqual(2, result.plan.total_actions)
            self.assertEqual(1, result.plan.native_actions)
            self.assertTrue(result.backup.path.is_dir())
            self.assertEqual(before_args, build_electron.sha256(output / "args.gn"))
            self.assertEqual(b"compiled-object", object_file.read_bytes())
            self.assertEqual(before_object.st_mtime_ns, object_file.stat().st_mtime_ns)
            actual_commands = [command for command in commands if "-n" not in command]
            self.assertEqual(
                [
                    (
                        str(layout.ninja),
                        "-C",
                        str(output),
                        "build.ninja.stamp",
                    )
                ],
                actual_commands,
            )

    def test_generator_failure_keeps_backup_and_does_not_restore(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            layout = self.layout(root)
            output = self.output_fixture(root, log_version=6)
            self.write_generator_manifest(output)
            (output / "gn_logs.txt").write_text("changed\n", encoding="utf-8")
            (output / "build.ninja.d").write_text(
                "build.ninja.stamp: ./gn_logs.txt\n", encoding="utf-8"
            )

            def fake_command(command, *, environment=None, cwd=None):
                if "-n" in command:
                    return "[0/1] Regenerating ninja files"
                (output / "gn_logs.txt").write_text(
                    "partially-updated\n", encoding="utf-8"
                )
                raise build_electron.BuildSafetyError("GN failed")

            with mock.patch.object(
                build_electron, "command_output", side_effect=fake_command
            ):
                with self.assertRaisesRegex(
                    build_electron.BuildSafetyError,
                    "no automatic restoration was attempted; backup:",
                ):
                    build_electron.recover_metadata(
                        layout,
                        output,
                        {"PATH": "/managed"},
                        {},
                        root / "metadata-backups",
                    )

            backups = list((root / "metadata-backups").iterdir())
            self.assertEqual(1, len(backups))
            self.assertEqual(
                "changed\n",
                (backups[0] / "files" / "gn_logs.txt").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "partially-updated\n",
                (output / "gn_logs.txt").read_text(encoding="utf-8"),
            )

    def test_recovery_rejects_changes_to_args_objects_or_metadata_compatibility(
        self,
    ) -> None:
        mutations = ("args", "object", "metadata")
        for mutation in mutations:
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as value,
            ):
                root = Path(value)
                layout = self.layout(root)
                output = self.output_fixture(root, log_version=6)
                self.write_generator_manifest(output)
                (output / "gn_logs.txt").write_text("changed\n", encoding="utf-8")
                (output / "build.ninja.d").write_text(
                    "build.ninja.stamp: ./gn_logs.txt\n", encoding="utf-8"
                )
                object_file = output / "obj" / "preserved.o"
                object_file.parent.mkdir()
                object_file.write_bytes(b"original")

                def fake_command(command, *, environment=None, cwd=None):
                    if "-n" in command and command[-1] == "build.ninja.stamp":
                        return "[0/1] Regenerating ninja files"
                    if command[-1] == "build.ninja.stamp":
                        if mutation == "args":
                            (output / "args.gn").write_text(
                                'target_cpu = "x64"\n', encoding="utf-8"
                            )
                        elif mutation == "object":
                            object_file.write_bytes(b"modified")
                        else:
                            (output / ".ninja_log").write_text(
                                "# ninja log v7\n", encoding="utf-8"
                            )
                        return "[0/1] Regenerating ninja files"
                    return "ninja: no work to do."

                with mock.patch.object(
                    build_electron, "command_output", side_effect=fake_command
                ):
                    with self.assertRaises(build_electron.BuildSafetyError):
                        build_electron.recover_metadata(
                            layout,
                            output,
                            {"PATH": "/managed"},
                            {},
                            root / "metadata-backups",
                        )

    def test_recovery_cli_exits_without_starting_a_build(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            layout = self.layout(root)
            output = self.output_fixture(root, log_version=6)
            backup = build_electron.MetadataBackup(path=root / "backup", files=tuple())
            result = build_electron.MetadataRecoveryResult(
                backup=backup,
                plan=build_electron.BuildPlan(12, 4, False),
                changed_metadata=("build.ninja",),
            )
            toolchain = build_electron.Toolchain(
                python=Path(sys.executable),
                node=root / "node",
                npm=root / "npm",
                developer_dir=root / "Xcode.app" / "Contents" / "Developer",
                sdk_path=root / "MacOSX.sdk",
                versions={},
            )
            versions = {"ninja": "1.12.1", "gn_sha256": "gn"}

            with contextlib.ExitStack() as stack:
                stack.enter_context(
                    mock.patch.object(
                        build_electron, "resolve_layout", return_value=layout
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        build_electron, "resolve_output", return_value=output
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        build_electron,
                        "output_lock",
                        return_value=contextlib.nullcontext(),
                    )
                )
                for name in (
                    "validate_output_metadata",
                    "validate_disk_space",
                    "detect_running_build",
                    "print_identity",
                    "validate_python_runtime",
                ):
                    stack.enter_context(mock.patch.object(build_electron, name))
                stack.enter_context(
                    mock.patch.object(
                        build_electron, "validate_pinned_tools", return_value=versions
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        build_electron, "validate_host_tools", return_value=toolchain
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        build_electron,
                        "build_environment",
                        return_value=contextlib.nullcontext({"PATH": "/managed"}),
                    )
                )
                stack.enter_context(
                    mock.patch.object(build_electron, "read_python_contract")
                )
                stack.enter_context(
                    mock.patch.object(
                        build_electron,
                        "regeneration_diagnostics",
                        return_value="generator diagnostics",
                    )
                )
                recover = stack.enter_context(
                    mock.patch.object(
                        build_electron, "recover_metadata", return_value=result
                    )
                )
                run_build = stack.enter_context(
                    mock.patch.object(build_electron, "run_build")
                )

                status = build_electron.main(("--mode", "user", "--regenerate-only"))

            self.assertEqual(0, status)
            recover.assert_called_once()
            run_build.assert_not_called()

    def test_codex_mode_has_non_overridable_action_limits(self) -> None:
        with self.assertRaisesRegex(
            build_electron.BuildSafetyError, "Codex build refused"
        ):
            build_electron.enforce_mode(
                "codex",
                build_electron.BuildPlan(501, 0, False),
                4,
                4.0,
            )
        with self.assertRaisesRegex(build_electron.BuildSafetyError, "native actions"):
            build_electron.enforce_mode(
                "codex",
                build_electron.BuildPlan(100, 101, False),
                4,
                4.0,
            )

    def test_codex_mode_limits_parallelism(self) -> None:
        plan = build_electron.BuildPlan(1, 1, False)
        with self.assertRaisesRegex(build_electron.BuildSafetyError, "jobs exceeds"):
            build_electron.enforce_mode("codex", plan, 5, 4.0)
        with self.assertRaisesRegex(build_electron.BuildSafetyError, "load average"):
            build_electron.enforce_mode("codex", plan, 4, 4.1)

    def test_user_mode_does_not_apply_codex_action_thresholds(self) -> None:
        build_electron.enforce_mode(
            "user",
            build_electron.BuildPlan(50_000, 40_000, False),
            16,
            16.0,
        )

    def test_codex_policy_blocks_direct_native_build_commands(self) -> None:
        for command in (
            "ninja -C ../out/JScene3D-Electron42 electron",
            "../third_party/depot_tools/autoninja.py -C ../out/JScene3D-Electron42 electron",
            "e build",
            "../../buildtools/mac/gn gen ../out/JScene3D-Electron42",
        ):
            with self.subTest(command=command):
                self.assertIsNotNone(pre_tool_use_policy.denial_reason(command))

    def test_codex_policy_allows_only_codex_guarded_mode(self) -> None:
        script = "/checkout/scripts/jscene3d/build-electron.sh"
        self.assertIsNone(
            pre_tool_use_policy.denial_reason(
                "{} --mode codex --preflight-only".format(script)
            )
        )
        self.assertIsNotNone(
            pre_tool_use_policy.denial_reason("{} --mode user".format(script))
        )
        self.assertIsNotNone(
            pre_tool_use_policy.denial_reason(
                "{} --mode codex && echo bypass".format(script)
            )
        )

    def test_codex_policy_blocks_metadata_recovery_in_every_mode(self) -> None:
        script = "/checkout/scripts/jscene3d/build-electron.sh"
        for command in (
            "{} --mode codex --regenerate-only".format(script),
            "{} --mode user --regenerate-only".format(script),
            "env SAFE=1 {} --mode codex --regenerate-only".format(script),
        ):
            with self.subTest(command=command):
                self.assertIsNotNone(pre_tool_use_policy.denial_reason(command))

    def test_codex_policy_does_not_block_unrelated_checks(self) -> None:
        self.assertIsNone(pre_tool_use_policy.denial_reason("git diff --check"))
        self.assertIsNone(pre_tool_use_policy.denial_reason("rg ninja scripts"))
        self.assertIsNone(
            pre_tool_use_policy.denial_reason(
                "python3 -m py_compile scripts/jscene3d/build_electron.py"
            )
        )

    def test_codex_policy_blocks_output_deletion_and_direct_python_entry(self) -> None:
        for name in (
            ".ninja_log",
            ".ninja_deps",
            "args.gn",
            "build.ninja",
            "build.ninja.d",
            "build.ninja.stamp",
            "toolchain.ninja",
            "gn_logs.txt",
        ):
            with self.subTest(name=name):
                self.assertIsNotNone(
                    pre_tool_use_policy.denial_reason("rm -f {}".format(name))
                )
        self.assertIsNotNone(
            pre_tool_use_policy.denial_reason(
                "python3 scripts/jscene3d/build_electron.py --mode codex"
            )
        )

    def test_environment_excludes_ambient_build_contaminants(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            layout = self.layout(root)
            depot_tools = layout.depot_tools
            depot_tools.mkdir(parents=True)
            layout.gn.parent.mkdir(parents=True)
            layout.gn.touch()
            layout.ninja.parent.mkdir(parents=True)
            layout.ninja.touch()
            node_dir = root / "node-bin"
            node_dir.mkdir()
            node = node_dir / "node"
            npm = node_dir / "npm"
            node.touch()
            npm.touch()
            toolchain = build_electron.Toolchain(
                python=Path(sys.executable),
                node=node,
                npm=npm,
                developer_dir=root / "Xcode.app" / "Contents" / "Developer",
                sdk_path=root / "MacOSX.sdk",
                versions={},
            )
            previous = os.environ.get("ELECTRON_RUN_AS_NODE")
            os.environ["ELECTRON_RUN_AS_NODE"] = "1"
            try:
                with build_electron.build_environment(layout, toolchain) as environment:
                    self.assertNotIn("ELECTRON_RUN_AS_NODE", environment)
                    self.assertNotIn("/opt/homebrew/bin", environment["PATH"])
                    self.assertIn(str(depot_tools), environment["PATH"])
                    shim = Path(environment["PATH"].split(os.pathsep)[0])
                    self.assertEqual(layout.gn.resolve(), (shim / "gn").resolve())
                    self.assertEqual(layout.ninja.resolve(), (shim / "ninja").resolve())
                    self.assertEqual(
                        Path(sys.executable).resolve(), (shim / "python3").resolve()
                    )
                    self.assertEqual(
                        str(layout.source_root / "buildtools"),
                        environment["CHROMIUM_BUILDTOOLS_PATH"],
                    )
            finally:
                if previous is None:
                    os.environ.pop("ELECTRON_RUN_AS_NODE", None)
                else:
                    os.environ["ELECTRON_RUN_AS_NODE"] = previous

    def test_python_contract_is_derived_from_checkout_owned_pins(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            layout = self.layout(root)
            layout.source_root.mkdir(parents=True)
            layout.depot_tools.joinpath("bootstrap").mkdir(parents=True)
            layout.source_root.joinpath(".vpython3").write_text(
                'python_version: "3.11"\n', encoding="utf-8"
            )
            layout.depot_tools.joinpath(".vpython3").write_text(
                'python_version: "3.11"\n', encoding="utf-8"
            )
            layout.depot_tools.joinpath("bootstrap", "manifest.txt").write_text(
                "@Subdir python3\n"
                "infra/3pp/tools/cpython3/${platform} "
                "version:2@3.11.8.chromium.35\n",
                encoding="utf-8",
            )

            contract = build_electron.read_python_contract(layout)

            self.assertEqual((3, 11), contract.version)
            self.assertEqual("2@3.11.8.chromium.35", contract.package_pin)

    def test_python_contract_rejects_disagreeing_checkout_specs(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            layout = self.layout(root)
            layout.source_root.mkdir(parents=True)
            layout.depot_tools.joinpath("bootstrap").mkdir(parents=True)
            layout.source_root.joinpath(".vpython3").write_text(
                'python_version: "3.11"\n', encoding="utf-8"
            )
            layout.depot_tools.joinpath(".vpython3").write_text(
                'python_version: "3.12"\n', encoding="utf-8"
            )
            layout.depot_tools.joinpath("bootstrap", "manifest.txt").write_text(
                "infra/3pp/tools/cpython3/${platform} version:2@3.11.8.chromium.35\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                build_electron.BuildSafetyError, "Python contracts disagree"
            ):
                build_electron.read_python_contract(layout)

    def test_python_runtime_probe_rejects_checkout_code_failure(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            layout = self.layout(root)
            generator = (
                layout.source_root
                / "tools"
                / "metrics"
                / "histograms"
                / "generate_allowlist_from_histograms_file.py"
            )
            generator.parent.mkdir(parents=True)
            generator.touch()
            contract = build_electron.PythonContract(
                version=(3, 11),
                package_pin="2@3.11.8.chromium.35",
            )

            with mock.patch.object(
                build_electron,
                "command_output",
                side_effect=(
                    "3.11.8\n/managed/python3",
                    build_electron.BuildSafetyError(
                        "unsupported operand type(s) for |: 'type' and 'type'"
                    ),
                ),
            ):
                with self.assertRaisesRegex(
                    build_electron.BuildSafetyError,
                    "checkout Python compatibility probe failed",
                ):
                    build_electron.validate_python_runtime(
                        layout,
                        contract,
                        Path("/managed/python3"),
                        {"PATH": "/managed"},
                    )

    def test_python_runtime_rejects_path_identity_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            layout = self.layout(root)
            contract = build_electron.PythonContract(
                version=(3, 11),
                package_pin="2@3.11.8.chromium.35",
            )

            with mock.patch.object(
                build_electron,
                "command_output",
                return_value="3.11.8\n/usr/bin/python3",
            ):
                with self.assertRaisesRegex(
                    build_electron.BuildSafetyError,
                    "expected checkout-managed",
                ):
                    build_electron.validate_python_runtime(
                        layout,
                        contract,
                        root / "managed" / "python3",
                        {"PATH": "/usr/bin"},
                    )

    def test_existing_checkout_managed_python_skips_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            layout = self.layout(root)
            executable = (
                layout.depot_tools
                / "bootstrap-2@3.11.8.chromium.35_bin"
                / "python3"
                / "bin"
                / "python3"
            )
            executable.parent.mkdir(parents=True)
            executable.touch(mode=0o755)
            layout.depot_tools.joinpath("python3_bin_reldir.txt").write_text(
                "bootstrap-2@3.11.8.chromium.35_bin/python3/bin\n",
                encoding="utf-8",
            )
            contract = build_electron.PythonContract(
                version=(3, 11),
                package_pin="2@3.11.8.chromium.35",
            )

            with mock.patch.object(build_electron, "command_output") as command:
                selected = build_electron.ensure_managed_python(layout, contract)

            command.assert_not_called()
            self.assertEqual(executable.resolve(), selected)

    def test_stale_checkout_managed_python_bootstraps_before_validation(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            layout = self.layout(root)
            layout.depot_tools.mkdir(parents=True)
            bootstrap = layout.depot_tools / "bootstrap_python3"
            bootstrap.touch()
            layout.depot_tools.joinpath("python3_bin_reldir.txt").write_text(
                "bootstrap-older-pin_bin/python3/bin\n",
                encoding="utf-8",
            )
            executable = (
                layout.depot_tools
                / "bootstrap-2@3.11.8.chromium.35_bin"
                / "python3"
                / "bin"
                / "python3"
            )
            contract = build_electron.PythonContract(
                version=(3, 11),
                package_pin="2@3.11.8.chromium.35",
            )

            def bootstrap_side_effect(*_args, **_kwargs):
                executable.parent.mkdir(parents=True)
                executable.touch(mode=0o755)
                layout.depot_tools.joinpath("python3_bin_reldir.txt").write_text(
                    "bootstrap-2@3.11.8.chromium.35_bin/python3/bin\n",
                    encoding="utf-8",
                )
                return ""

            with mock.patch.object(
                build_electron,
                "command_output",
                side_effect=bootstrap_side_effect,
            ) as command:
                selected = build_electron.ensure_managed_python(layout, contract)

            self.assertEqual("/bin/bash", command.call_args.args[0][0])
            self.assertEqual(str(bootstrap), command.call_args.args[0][-1])
            self.assertEqual(executable.resolve(), selected)

    def test_generated_graph_rejects_python_outside_path_contract(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            output = Path(value)
            (output / "build.ninja").write_text(
                "rule good\n  command = python3 ../../good.py\n",
                encoding="utf-8",
            )
            (output / "toolchain.ninja").write_text(
                "rule bad\n  command = /usr/bin/python3 ../../bad.py\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                build_electron.BuildSafetyError, "unsupported Python executable"
            ):
                build_electron.validate_generated_python_commands(output)

    @staticmethod
    def layout(root: Path):
        electron = root / "src" / "electron"
        source = root / "src"
        return build_electron.Layout(
            electron_root=electron,
            source_root=source,
            output_root=source / "out",
            depot_tools=source / "third_party" / "depot_tools",
            ninja=source / "third_party" / "ninja" / "ninja",
            gn=source / "buildtools" / "mac" / "gn",
            clang=source / "third_party" / "llvm" / "clang",
            rustc=source / "third_party" / "rust" / "rustc",
        )

    @staticmethod
    def output_fixture(root: Path, log_version: int) -> Path:
        output = root / "out" / "fixture"
        output.mkdir(parents=True)
        (output / "args.gn").write_text(
            'import("//electron/build/args/testing.gn")\ntarget_cpu = "arm64"\n',
            encoding="utf-8",
        )
        (output / "build.ninja").write_text(
            "rule gn\n  command = ../../buildtools/mac/gn --regeneration gen .\n",
            encoding="utf-8",
        )
        for name in ("build.ninja.d", "build.ninja.stamp", "toolchain.ninja"):
            (output / name).touch()
        (output / ".ninja_log").write_text(
            "# ninja log v{}\n".format(log_version), encoding="utf-8"
        )
        (output / ".ninja_deps").write_bytes(b"# ninjadeps\n" + struct.pack("<I", 4))
        return output

    @staticmethod
    def write_generator_manifest(output: Path) -> None:
        (output / "build.ninja").write_text(
            "ninja_required_version = 1.7.2\n\n"
            "rule gn\n"
            "  command = ../../buildtools/mac/gn --root=../.. -q "
            "--regeneration gen .\n"
            "  pool = console\n"
            "  description = Regenerating ninja files\n\n"
            "build build.ninja.stamp: gn\n"
            "  generator = 1\n"
            "  depfile = build.ninja.d\n\n"
            "build build.ninja: phony build.ninja.stamp\n"
            "  generator = 1\n",
            encoding="utf-8",
        )

    @staticmethod
    def synthetic_build_command(
        root: Path,
        ninja_source: str,
        *,
        extra_environment: dict[str, str] | None = None,
    ) -> list[str]:
        """Create a fixture Ninja and wrapper harness without touching outputs."""
        ninja = root / "synthetic-ninja"
        ninja.write_text(ninja_source, encoding="utf-8")
        ninja.chmod(0o755)
        harness = root / "run-synthetic-build.py"
        harness.write_text(
            """from pathlib import Path
from types import SimpleNamespace
import os
import sys

sys.path.insert(0, {!r})
from build_electron import run_build

environment = dict(os.environ)
status = run_build(
    SimpleNamespace(ninja=Path(sys.argv[1])),
    Path(sys.argv[2]),
    ("electron",),
    2,
    2.0,
    environment,
)
raise SystemExit(status)
""".format(str(SCRIPT.parent)),
            encoding="utf-8",
        )
        environment = dict(os.environ)
        if extra_environment:
            environment.update(extra_environment)
        launcher = root / "launch-synthetic-build.py"
        launcher.write_text(
            """import os
import sys

os.execve(sys.argv[1], sys.argv[1:], {!r})
""".format(environment),
            encoding="utf-8",
        )
        return [
            sys.executable,
            str(launcher),
            sys.executable,
            str(harness),
            str(ninja),
            str(root / "unused-output"),
        ]


if __name__ == "__main__":
    unittest.main()
