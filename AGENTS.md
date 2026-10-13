# JScene3D Electron Agent Instructions

These rules supplement `CLAUDE.md` for automated coding agents.

## Native build safety

- Never run raw `ninja`, `autoninja`, `autoninja.py`, `e build`, `gn gen`, or
  `gn clean` commands.
- Never clean, regenerate, replace, or delete Chromium/Electron native output
  metadata, including `.ninja_log`, `.ninja_deps`, `build.ninja`, and
  `args.gn`, unless the user explicitly authorizes a separate recovery task.
- Never substitute Homebrew Ninja, GN, Clang, Rust, Node, or Python tools for
  the validated tools selected by the JScene3D build entry point.
- Treat checkout-owned tool contracts and executable compatibility probes as
  authoritative. Do not infer compatibility from a remembered minimum version
  or a tool's version string alone.
- Codex may invoke only
  `scripts/jscene3d/build-electron.sh --mode codex --build ...` for a native
  build. Omitting `--build` performs the read-only preflight.
- Codex must stop if the guarded plan exceeds 500 total actions or 100 native
  compilation actions. Only the user may invoke `--mode user`.
- A request to verify Electron changes does not implicitly authorize a full or
  unexpectedly large Chromium/Electron build.
- Do not terminate unrelated native build processes. Diagnose ownership first.
- Do not leave Electron applications or native build processes running after
  an explicitly authorized manual acceptance check.

The guarded build entry point must preserve existing outputs and must never
clean or regenerate them implicitly.
