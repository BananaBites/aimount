# aimount

`aim` is a minimal local-first CLI that starts a persistent Docker workspace for autonomous coding agents while only exposing host resources you explicitly allow.

It is a developer convenience/containment tool, not a hardened sandbox.

## Install

```bash
pip install .
# or
pipx install .
```

## Use

From a project directory:

```bash
aim
```

On first run this creates `.aim/Dockerfile` and `.aim/config.toml`, builds an Ubuntu-based image, starts/reuses a named container, mounts the current project at the same absolute path, and drops you into `bash`.

Useful commands:

```bash
aim init
aim rebuild
aim reset
aim clean
aim allow ssh github-agent
aim allow auth codex
aim allow dir ~/Downloads --ro
aim allow port 3000
aim network off
aim network host
aim list
aim doctor
```

Config/state lives in:

- project: `.aim/`
- user: `~/.aim/`
