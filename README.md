# AiMount

[![tests](https://github.com/BananaBites/aimount/actions/workflows/tests.yml/badge.svg)](https://github.com/BananaBites/aimount/actions/workflows/tests.yml)

AiMount (`aim`) is a minimal local-first CLI that starts a persistent Docker workspace
for autonomous coding agents
while only sharing host resources you explicitly choose.

It is a developer convenience/containment tool, not a hardened sandbox.


## Security model

AiMount reduces accidental host access by putting agent tools in a Docker
workspace and mounting only selected host paths. It is not a security boundary
against malicious code or hostile container workloads.

Important implications:

- The current project is mounted read-write into the container, except paths in
  `[share].readonly` such as `.aim/`.
- Shared agents, SSH keys, directories, and files can expose credentials or
  private data to tools running in the container.
- On Linux, the default `network.mode = "auto"` uses host networking for
  convenience. Use `aim network bridge` or `aim network off` for more isolation.
- Container-local changes outside mounted paths can be discarded when the
  container is rebuilt or updated. Put persistent setup in `.aim/Dockerfile`.

Run `aim doctor` to review sharing and isolation warnings for a project.


## Install

From a local checkout:

```bash
pip install .
# or
pipx install .
```

Directly from GitHub:

```bash
pip install git+https://github.com/BananaBites/aimount.git
# or
pipx install git+https://github.com/BananaBites/aimount.git
```

For private repositories, use SSH:

```bash
pip install git+ssh://git@github.com/BananaBites/aimount.git
# or
pipx install git+ssh://git@github.com/BananaBites/aimount.git
```

Pinned to a tag/branch:

```bash
pip install git+ssh://git@github.com/BananaBites/aimount.git@v0.5.1
```

Update to the latest version:

```bash
aim update
```

`aim update` checks GitHub version tags and reinstalls the newest tag using the
same Python package manager style it detects for the current install: `pipx` when
AiMount is running from a pipx venv, otherwise `python -m pip`. It reuses the
installed Git URL/transport where possible, so SSH installs keep using SSH.

Manual equivalent:

```bash
pip install --upgrade --force-reinstall git+ssh://git@github.com/BananaBites/aimount.git@TAG
# or, for pipx
pipx install --force git+ssh://git@github.com/BananaBites/aimount.git@TAG
```


## Use

From a project directory:

```bash
aim
```

When starting, `aim` checks for newer GitHub version tags at most once per day
and prints a short `aim update` reminder if an update is available.

On first run this creates `.aim/Dockerfile` and `.aim/config.toml`,
builds an Ubuntu-based image, starts/reuses a named container,
mounts the current project at the same absolute path, overlays `[share].readonly`
paths like `.aim/` read-only, and drops you into `bash`.

The default Dockerfile installs common agent CLIs globally:

- `pi` (`@earendil-works/pi-coding-agent`)
- `codex` (`@openai/codex`)
- `claude` (`@anthropic-ai/claude-code`)
- `gemini` (`@google/gemini-cli`)

To refresh these preinstalled tools, run `aim update-container` from outside the
container. It rebuilds the image, replaces the project container on success, and
warns first if it detects container-local system changes such as manual `apt`
installs. Put persistent package installs in `.aim/Dockerfile` instead.


## Shell completion

Try completion only for the current shell session:

```bash
# bash/zsh
eval "$(aim --show-completion)"

# fish
aim --show-completion | source
```

Persistent installation is available via:

```bash
aim --install-completion
```

Typer does not provide an uninstall command. To find installed completion snippets/files:

```bash
grep -R "aim" ~/.bashrc ~/.zshrc ~/.bash_completion ~/.local/share/bash-completion ~/.config/fish/completions 2>/dev/null
```


## Commands

```bash
aim
aim --version
aim init
aim init --force          # overwrite .aim/Dockerfile with current default
aim update                # update aim itself when a newer GitHub tag exists
aim update --check        # check only; exits non-zero if an update exists
aim status                # show project/config/container status
aim run pi                # run a command inside the workspace
aim run -- pi --help      # use -- before command flags
aim rebuild
aim update-container     # refresh image tools; warns about container-local system changes
aim update-container -y  # skip the warning prompt
aim reset
aim clean
aim clean --all           # remove all aim containers/images; prompts first
aim clean --all --force   # same, without prompt

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
aim share file ~/.gitconfig --ro

aim expose port 3000
aim network off
aim network host          # default on Linux via network=auto
aim network bridge        # more isolation; use expose port for dev servers
aim list
aim doctor                # checks Docker, shares, and outside-project symlinks
```


## Recommended Pi workflow

```bash
aim init
aim share agent pi --host
aim run pi
```

This exposes only the current project and your real `~/.pi` directory to Pi.
If the command you run has its own flags, put `--` before it:

```bash
aim run -- pi --help
```


## Sharing model

- Shares are project-local and written to `.aim/config.toml`.
- `share agent NAME` shares persistent state/config for an AI CLI.
- `aim share agent pi` mounts `~/.aim/share/agents/pi` as `~/.pi` in the container.
- `aim share agent pi --host` mounts your real host `~/.pi` as `~/.pi` in the container.
- `share ssh` shares SSH identity/config as `~/.ssh`.
- `share dir` and `share file` add paths to `[share].readwrite` by default or
  `[share].readonly` with `--ro`.
- `--host` means use the real host location instead of aim-managed storage.
- The project root itself is always mounted read-write.
- Relative share paths are resolved inside the project; absolute paths use the
  same host/container path.
- More specific paths override broader mounts, so `[share].readwrite` can re-open
  a subpath of `[share].readonly`.
- `[share].readonly` paths stay visible but read-only; `.aim` is readonly by default.
- `[share].hidden` only has an effect inside an already mounted path; `.env` and
  `.env.local` are hidden by default.

Config/state lives in:

- project config/container definition: `.aim/`
- reusable private shared data: `~/.aim/share/`
- user config: `~/.aim/config.toml` is reserved for future defaults


## License

MIT. See [LICENSE](LICENSE).
