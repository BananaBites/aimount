# aimount

`aim` is a minimal local-first CLI that starts a persistent Docker workspace for autonomous coding agents while only sharing host resources you explicitly choose.

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

The default Dockerfile installs common agent CLIs globally:

- `pi` (`@earendil-works/pi-coding-agent`)
- `codex` (`@openai/codex`)
- `claude` (`@anthropic-ai/claude-code`)
- `gemini` (`@google/gemini-cli`)

## Commands

```bash
aim
aim init
aim init --force          # overwrite .aim/Dockerfile with current default
aim run pi                # run a command inside the workspace
aim rebuild
aim reset
aim clean

aim share agent pi        # ~/.aim/share/agents/pi -> ~/.pi
aim share agent codex     # ~/.aim/share/agents/codex -> ~/.codex
aim share agent claude
aim share agent gemini
aim share agent pi --host # ~/.pi -> ~/.pi
aim share list            # show project shares from .aim/config.toml

aim unshare agent pi
aim unshare ssh
aim unshare dir ~/Downloads
aim unshare file ~/.gitconfig

aim share ssh             # ~/.aim/share/ssh -> ~/.ssh
aim share ssh --host      # ~/.ssh -> ~/.ssh

aim share dir ~/Downloads --ro
aim share file ~/.gitconfig --target ~/.gitconfig --ro

aim expose port 3000
aim network off
aim network host          # default on Linux via network=auto
aim network bridge        # more isolation; use expose port for dev servers
aim list
aim doctor                # checks Docker, shares, and outside-project symlinks
```

## Sharing model

- Shares are project-local and written to `.aim/config.toml`.
- `share agent NAME` shares persistent state/config for an AI CLI.
- `share ssh` shares SSH identity/config as `~/.ssh`.
- `share dir` and `share file` share real host paths.
- `--host` means use the real host location instead of aim-managed storage.

Config/state lives in:

- project config/container definition: `.aim/`
- reusable private shared data: `~/.aim/share/`
- user config: `~/.aim/config.toml` is reserved for future defaults
