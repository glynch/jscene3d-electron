# JScene3D Electron build entry point

`build-electron.sh` is the only supported entry point for local JScene3D
Electron native builds. It preserves the existing Chromium output, never
cleans it and refuses implicit regeneration. An explicit user-only recovery
operation is available for a reviewed stale-manifest condition.

The script is independent of the current working directory. It validates the
repository-pinned Ninja, GN, Clang and Rust tools; selects the Node major in
`.nvmrc`; constructs a sanitized child `PATH`; checks the existing Ninja/GN
metadata; and obtains a pinned-Ninja dry-run plan before any build begins.

Python is not selected from the invoking shell or from `/usr/bin`. The entry
point cross-checks Chromium's `.vpython3`, depot_tools' `.vpython3` and the
exact CPython package in depot_tools' bootstrap manifest. If that pinned
runtime is not installed inside this checkout, the entry point bootstraps it
before inspecting the build. It then verifies that `python3` in the sanitized
build environment resolves to that exact executable and runs a real
checkout-owned Chromium generator as a compatibility probe. A version string
alone is not considered proof that the Python runtime is compatible.

The generated Ninja graph is also checked so Python build actions use the
guarded `python3` command rather than an absolute, ambient or separately
versioned interpreter. Any Python bootstrap, contract, identity or
compatibility failure stops before the Ninja plan or compilation begins and
names the failing contract/probe.

Preflight-only behavior is the default. It invokes pinned Ninja with `-n` to
inspect the action plan and does not compile, link, clean or regenerate
anything. `--preflight-only` remains available when an explicit command is
preferred. Starting a build requires `--build`.

## User-operated metadata recovery

When normal preflight reports that Ninja must regenerate its manifests, review
the reported generator dependency diagnostics first. If regeneration has been
approved, run:

```bash
/absolute/path/to/electron/scripts/jscene3d/build-electron.sh \
  --mode user \
  --regenerate-only \
  --output JScene3D-Electron42
```

Recovery validates and dry-runs the existing `build.ninja.stamp` generator
edge, creates a checksum manifest and narrowly scoped metadata backup under
`out/.jscene3d-metadata-backups`, and then asks pinned Ninja to build only that
stamp target. It verifies that `args.gn`, compiled objects and tool identities
remain unchanged, validates the regenerated metadata and prints a new
read-only `electron` plan. It never continues into compilation or linking.

If generation fails, the command reports the unique backup directory and
stops. It does not attempt an automatic restoration. `--regenerate-only` is
incompatible with Codex mode, preflight mode, explicit targets and concurrency
options.

## User-operated continuation

Run the read-only preflight first:

```bash
/absolute/path/to/electron/scripts/jscene3d/build-electron.sh \
  --mode user \
  --preflight-only \
  --output JScene3D-Electron42 \
  --target electron
```

If it passes, continue the existing incremental output:

```bash
/absolute/path/to/electron/scripts/jscene3d/build-electron.sh \
  --mode user \
  --build \
  --output JScene3D-Electron42 \
  --target electron \
  --jobs 8 \
  --load-average 8
```

User mode permits a plan larger than the agent thresholds but retains every
toolchain, metadata and no-regeneration safety check.

During execution, Ninja inherits the launcher's stdout and stderr so its
native terminal progress and diagnostics remain real-time in both interactive
and redirected use. The launcher waits for Ninja to terminate, then prints a
wrapper-owned success or failure summary with the target and final shell exit
status. Interrupts are forwarded to Ninja's process group so active build
children receive the same termination signal.

## Codex mode

Codex may use only `--mode codex`. Preflight is the default; an allowed small
build still requires `--build`. The entry point refuses plans exceeding 500
total actions or 100 native compilation actions, and caps requested concurrency
at four jobs with a load average of four. There is no override flag.

The repository-local `PreToolUse` hook blocks raw Ninja, autoninja, `e build`,
GN generation, output deletion and user-mode build-script invocation. Codex
loads repository hooks only after the project and hook definition are trusted;
review and trust it with `/hooks`. For centrally enforced policy, install the
same hook as a managed Codex hook outside agent-writable paths.
