# JScene3D Electron

JScene3D Electron is a customized fork of
[Electron](https://github.com/electron/electron) that provides the native
viewport integration required by the JScene3D Editor. It is a supporting
component of the JScene3D ecosystem, not a standalone application or a
general-purpose Electron distribution.

The fork is based on **Electron 42.10.0**.

## Why this fork exists

The JScene3D Editor runs on a Code OSS workbench while scene rendering remains
owned by the Java JScene3D engine. Standard Electron supplies the desktop
runtime and SharedTexture presentation primitives, but it does not manage the
JScene3D Java renderer process or transfer an editor-owned native render surface
to that process.

This fork adds a narrow integration layer that:

- launches and manages a Java renderer process for each native viewport;
- creates and owns the viewport's native render surface;
- transfers macOS IOSurface access to the Java child through Mach ports;
- exposes the IOSurface through Electron's existing SharedTexture facilities;
- routes viewport commands and renderer events between Code OSS and Java; and
- carries Scene selection and interactive transform events across the native
  boundary.

Scene evaluation and drawing stay in JScene3D's Java/LWJGL/OpenGL renderer.
Code OSS presents the completed frames and owns the editor interaction model;
it does not maintain a second browser-based scene renderer.

## Architecture

```text
JScene3D Editor (Code OSS)
    |  validated workbench IPC
    v
JScene3D Electron main process
    |  jscene3dRenderer API
    v
Native C++ session and process bridge
    |  stdio protocol + Mach rendezvous/control channel
    v
Java JScene3D renderer process
    |  LWJGL/OpenGL rendering
    v
Electron-owned macOS IOSurface
    |  SharedTexture -> VideoFrame -> WebGPU presentation
    v
Code OSS Scene View
```

The major responsibilities are separated as follows:

- **Code OSS** maps each viewport pane to an exact renderer session, validates
  IPC payloads, transfers frames into the workbench, and rejects stale session,
  surface-generation, and frame identities.
- **The [`jscene3dRenderer` Electron API](lib/browser/api/jscene3d-renderer.ts)**
  exposes structured launch, surface, messaging, pause/resume, resize, state,
  and shutdown operations to Electron main-process code.
- **The native C++ bridge** validates launch requests, owns renderer sessions,
  starts the Java child, parses renderer events, manages IOSurfaces, and routes
  callbacks to JavaScript.
- **The Java renderer process** owns the live JScene3D scene, editor camera,
  rendering state, selection/picking, and transform-gizmo behavior for one
  viewport.
- **The macOS transport** uses Mach port rendezvous for the initial IOSurface
  and a native control channel for replacement surfaces after resize.

Electron remains the owner of each IOSurface. The Java child acquires its own
process-local reference to the same shared resource; framebuffer pixels are not
sent through the line-oriented command protocol. Code OSS imports the resulting
surface using Electron's SharedTexture implementation and presents it as a
`VideoFrame` through WebGPU.

The sandboxed Code OSS preload reaches Electron main through validated
`vscode:jscene3dViewport:*` IPC channels. Electron main checks the calling
window, pane, session, and payload identities before forwarding commands to
`jscene3dRenderer`. The Java line protocol remains at development version
`1.0`; separate contract and build identities reject stale development runtime
artifacts without continually changing that protocol version.

The detailed guarded-build workflow is documented in
[`scripts/jscene3d/README.md`](scripts/jscene3d/README.md). Architecture and
product behavior spanning the other repositories are documented with those
components.

## Key capabilities

- One isolated Java renderer process and native surface per viewport session.
- Structured renderer launch with validated executable, class path, working
  directory, native-library path, arguments, and initial dimensions.
- IOSurface creation, Mach-right transfer, SharedTexture exposure, and
  generation-safe surface replacement on resize.
- Renderer session states for startup, running, paused, failed, stopping, and
  stopped lifecycles.
- Bidirectional command and event transport for frames, scene snapshots,
  navigation, selection, picking, and interactive transforms.
- Independent session identity and event routing for multiple viewports.
- Validation and rejection of malformed native renderer events.
- Cooperative renderer shutdown with bounded forced termination and process
  reaping when a child does not exit.
- Application-shutdown cleanup for all remaining renderer sessions.

## Building

The only supported local native-build entry point is:

```text
scripts/jscene3d/build-electron.sh
```

The launcher is independent of the current working directory. It validates the
existing output metadata and repository-pinned Ninja and GN binaries, selects
the checkout-managed Python runtime and the Node major declared by `.nvmrc`,
validates the Clang and Rust toolchains, and constructs a sanitized child
environment. It then obtains a pinned-Ninja dry-run plan before allowing any
compilation.

Preflight is the default and does not compile, link, clean, or regenerate:

```bash
./scripts/jscene3d/build-electron.sh \
  --mode user \
  --output JScene3D-Electron42 \
  --target electron
```

After reviewing a successful plan, a developer can continue the existing
incremental output explicitly:

```bash
./scripts/jscene3d/build-electron.sh \
  --mode user \
  --build \
  --output JScene3D-Electron42 \
  --target electron \
  --jobs 8 \
  --load-average 8
```

If preflight reports that Ninja metadata must be regenerated, recovery is a
separate, user-operated operation:

```bash
./scripts/jscene3d/build-electron.sh \
  --mode user \
  --regenerate-only \
  --output JScene3D-Electron42
```

Recovery backs up and validates narrowly scoped metadata, regenerates only the
existing manifest stamp, and stops without compiling. The build entry point
never cleans or implicitly regenerates the output directory.

Full native builds are user-operated. Automated Codex work may use only
`--mode codex`; builds still require explicit `--build`, are limited to guarded
incremental plans, and are refused above the repository's action and
concurrency limits. See the
[build documentation](scripts/jscene3d/README.md) for the complete safety
policy, diagnostics, and recovery procedure.

## Related repositories

- [JScene3D Engine](https://github.com/glynch/jscene3d) contains the Java
  engine, project and authoring model, renderer process, and macOS IOSurface
  Java/JNI integration.
- [JScene3D Editor](https://github.com/glynch/jscene3d-editor) contains the Code
  OSS product fork, built-in JScene3D extension, workbench viewport lifecycle,
  and presentation layer.
- [Upstream Electron](https://github.com/electron/electron) is the desktop
  runtime from which this repository is derived.

## Development status

This repository is under active development alongside the JScene3D engine and
editor. The current fork targets Electron 42.10.0 and has been developed and
verified on **macOS ARM64**. The JScene3D native viewport integration does not
currently claim Windows, Linux, or macOS Intel support.

The fork is not presented as production-ready or as a generally distributed
Electron release. Build outputs are currently development artifacts consumed
by the JScene3D Editor development workflow.

## Upstream attribution and license

This repository is derived from the
[Electron project](https://github.com/electron/electron). Electron, Chromium,
Node.js, and their contributors provide the underlying desktop runtime; the
JScene3D-specific work is a focused downstream integration.

The repository remains licensed under the [MIT License](LICENSE). Existing
copyright notices, third-party licenses, and upstream attribution continue to
apply.
