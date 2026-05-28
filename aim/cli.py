from __future__ import annotations

import getpass
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape

app = typer.Typer(
    invoke_without_command=True,
    no_args_is_help=False,
    help="Launch a small Docker workspace for autonomous coding agents.",
)
share_app = typer.Typer(help="Share persistent host resources with the workspace.")
unshare_app = typer.Typer(help="Stop sharing persistent host resources with the workspace.")
expose_app = typer.Typer(help="Expose development ports.")
network_app = typer.Typer(help="Configure workspace networking.")
app.add_typer(share_app, name="share")
app.add_typer(unshare_app, name="unshare")
app.add_typer(expose_app, name="expose")
app.add_typer(network_app, name="network")
console = Console()

PACKAGE_NAME = "aimount"
DEFAULT_REPO_URL = "https://github.com/BananaBites/aimount.git"
UPDATE_CHECK_INTERVAL = 24 * 60 * 60
UPDATE_CHECK_TIMEOUT = 2.0
CONFIG_VERSION = 1
GLOBAL_DIR = Path.home() / ".aim"
PROJECT_DIRNAME = ".aim"
DEFAULT_PORTS = [3000, 5173, 8000, 8080]
DEFAULT_HIDDEN = [".env", ".env.local"]
DEFAULT_READONLY = [".aim"]
AGENT_DIRS = {
    "pi": ".pi",
    "codex": ".codex",
    "claude": ".claude",
    "gemini": ".gemini",
}

DOCKERFILE = r"""# aim default Dockerfile v2
FROM ubuntu:24.04

# Fallbacks only. AiMount overrides these with your host username/UID/GID at build time.
ARG USERNAME=aim
ARG UID=1000
ARG GID=1000

ENV DEBIAN_FRONTEND=noninteractive \
    RUSTUP_HOME=/opt/rustup \
    CARGO_HOME=/opt/cargo \
    PATH=/opt/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates sudo git curl wget gnupg ripgrep fd-find jq tree vim nano tmux htop \
    build-essential cmake python3 python3-pip python3-venv sqlite3 unzip zip rsync \
    openssh-client bash-completion less locales pkg-config \
  && rm -rf /var/lib/apt/lists/* \
  && ln -sf /usr/bin/fdfind /usr/local/bin/fd

RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
  && apt-get install -y --no-install-recommends nodejs \
  && rm -rf /var/lib/apt/lists/*

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
  | CARGO_HOME=/opt/cargo RUSTUP_HOME=/opt/rustup sh -s -- -y --profile minimal --no-modify-path \
  && chmod -R a+rwX /opt/cargo /opt/rustup

RUN set -eux; \
    if getent group "${GID}" >/dev/null; then group_name="$(getent group "${GID}" | cut -d: -f1)"; \
    else groupadd --gid "${GID}" "${USERNAME}" && group_name="${USERNAME}"; fi; \
    if getent passwd "${UID}" >/dev/null; then \
      old_user="$(getent passwd "${UID}" | cut -d: -f1)"; \
      if [ "${old_user}" != "${USERNAME}" ]; then usermod -l "${USERNAME}" "${old_user}"; fi; \
      usermod -d "/home/${USERNAME}" -m "${USERNAME}" || true; \
      usermod -s /bin/bash "${USERNAME}"; \
    else \
      useradd --uid "${UID}" --gid "${GID}" -m -s /bin/bash "${USERNAME}"; \
    fi; \
    usermod -g "${GID}" "${USERNAME}"; \
    mkdir -p "/home/${USERNAME}"; \
    chown -R "${UID}:${GID}" "/home/${USERNAME}"; \
    mkdir -p "/home/${USERNAME}/.npm-global"; \
    chown -R "${UID}:${GID}" "/home/${USERNAME}/.npm-global"; \
    echo "${USERNAME} ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/${USERNAME}"; \
    chmod 0440 "/etc/sudoers.d/${USERNAME}"

ENV NPM_CONFIG_PREFIX=/home/${USERNAME}/.npm-global \
    PATH=/home/${USERNAME}/.npm-global/bin:/opt/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN printf '%s\n' \
  'export PATH="$HOME/.npm-global/bin:/opt/cargo/bin:$PATH"' \
  'if [ -n "$PS1" ]; then' \
  '  export PS1="\[\e[1;36m\]aim\[\e[0m\] \[\e[1;32m\]\u@\h\[\e[0m\]:\[\e[1;34m\]\w\[\e[0m\]\\$ "' \
  'fi' > /etc/profile.d/aim.sh

USER ${USERNAME}

ARG AIM_TOOLS_REFRESH=0
RUN echo "aim tools refresh: ${AIM_TOOLS_REFRESH}" >/tmp/aim-tools-refresh \
  && npm install -g \
    @earendil-works/pi-coding-agent \
    @openai/codex \
    @anthropic-ai/claude-code \
    @google/gemini-cli \
  && npm cache clean --force

WORKDIR /home/${USERNAME}
CMD ["bash"]
"""

PROJECT_CONFIG = """# Project-local aim config. Safe to commit/share.
config_version = 1

[network]
mode = "auto"   # auto, host, bridge, off
ports = [3000, 5173, 8000, 8080]

# Optional: override the container user.
# By default aim mirrors your host username and UID/GID so files stay editable.
[container]
# username = "dev"
# uid = 1000
# gid = 1000

[share]
agents = []
ssh = { enabled = false, host = false, readonly = false }
# Hints:
# - The project root itself is always mounted read-write.
# - Relative paths are resolved inside the project; absolute paths use the same host/container path.
# - More specific paths override broader mounts, so readwrite can re-open a subpath of readonly.
# - hidden only has an effect inside an already mounted path.
# Mounted read-only.
readonly = [".aim"]
# Mounted read-write. Absolute paths can add extra host paths; the project root is already read-write.
readwrite = []
# Hidden in the container by default to avoid exposing local secrets.
hidden = [".env", ".env.local"]
"""

GLOBAL_CONFIG = """# User-local aim config. Reserved for future personal defaults.
# Active shares are project-local in .aim/config.toml.
"""

LEGACY_UID_BLOCK = '''RUN set -eux; \\
    if getent group "${GID}" >/dev/null; then group_name="$(getent group "${GID}" | cut -d: -f1)"; \\
    else groupadd --gid "${GID}" "${USERNAME}" && group_name="${USERNAME}"; fi; \\
    useradd --uid "${UID}" --gid "${GID}" -m -s /bin/bash "${USERNAME}"; \\
    echo "${USERNAME} ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/${USERNAME}"; \\
    chmod 0440 "/etc/sudoers.d/${USERNAME}"
'''

FIXED_UID_BLOCK = '''RUN set -eux; \\
    if getent group "${GID}" >/dev/null; then group_name="$(getent group "${GID}" | cut -d: -f1)"; \\
    else groupadd --gid "${GID}" "${USERNAME}" && group_name="${USERNAME}"; fi; \\
    if getent passwd "${UID}" >/dev/null; then \\
      old_user="$(getent passwd "${UID}" | cut -d: -f1)"; \\
      if [ "${old_user}" != "${USERNAME}" ]; then usermod -l "${USERNAME}" "${old_user}"; fi; \\
      usermod -d "/home/${USERNAME}" -m "${USERNAME}" || true; \\
      usermod -s /bin/bash "${USERNAME}"; \\
    else \\
      useradd --uid "${UID}" --gid "${GID}" -m -s /bin/bash "${USERNAME}"; \\
    fi; \\
    usermod -g "${GID}" "${USERNAME}"; \\
    mkdir -p "/home/${USERNAME}"; \\
    chown -R "${UID}:${GID}" "/home/${USERNAME}"; \\
    echo "${USERNAME} ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/${USERNAME}"; \\
    chmod 0440 "/etc/sudoers.d/${USERNAME}"
'''

LEGACY_TOOLS_BLOCK = '''USER ${USERNAME}

RUN npm install -g \\
    @earendil-works/pi-coding-agent \\
'''

TOOLS_REFRESH_BLOCK = '''USER ${USERNAME}

ARG AIM_TOOLS_REFRESH=0
RUN echo "aim tools refresh: ${AIM_TOOLS_REFRESH}" >/tmp/aim-tools-refresh \\
  && npm install -g \\
    @earendil-works/pi-coding-agent \\
'''

SYSTEM_CHANGE_PREFIXES = (
    "/usr",
    "/etc",
    "/opt",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/var/lib/apt",
    "/var/lib/dpkg",
    "/var/log/apt",
)

DOCKER_MANAGED_PATHS = (
    "/etc/hostname",
    "/etc/hosts",
    "/etc/resolv.conf",
)

NOISY_PARENT_CHANGE_PATHS = (
    "/etc",
    "/usr",
    "/usr/bin",
    "/usr/lib",
    "/usr/local",
    "/var",
    "/var/lib",
    "/var/log",
    "/opt",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
)


def project_root() -> Path:
    return Path.cwd().resolve()


def project_aim_dir(root: Path) -> Path:
    return root / PROJECT_DIRNAME


def project_config_path(root: Path) -> Path:
    return project_aim_dir(root) / "config.toml"


def dockerfile_path(root: Path) -> Path:
    return project_aim_dir(root) / "Dockerfile"


def global_config_path() -> Path:
    return GLOBAL_DIR / "config.toml"


def ensure_global() -> None:
    (GLOBAL_DIR / "share" / "agents").mkdir(parents=True, exist_ok=True)
    (GLOBAL_DIR / "share" / "ssh").mkdir(parents=True, exist_ok=True)
    cfg = global_config_path()
    if not cfg.exists():
        cfg.write_text(GLOBAL_CONFIG)


def update_cache_path() -> Path:
    return GLOBAL_DIR / "update-check.json"


def current_version() -> str:
    # When running from a checkout, pyproject.toml is the source of truth and can
    # be newer than stale local egg-info metadata. Installed packages normally do
    # not include pyproject.toml, so fall back to package metadata there.
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    if pyproject.exists():
        try:
            return str(load_toml(pyproject).get("project", {}).get("version") or "0.0.0")
        except Exception:
            pass
    try:
        return importlib.metadata.version(PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0"


def parse_version(value: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def is_newer_version(candidate: str, current: str) -> bool:
    candidate_version = parse_version(candidate)
    current_version_tuple = parse_version(current)
    return bool(candidate_version and current_version_tuple and candidate_version > current_version_tuple)


def direct_url_metadata() -> dict[str, Any]:
    try:
        dist = importlib.metadata.distribution(PACKAGE_NAME)
        text = dist.read_text("direct_url.json")
    except importlib.metadata.PackageNotFoundError:
        return {}
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def file_url_to_path(url: str) -> Path | None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "file":
        return None
    return Path(urllib.request.url2pathname(parsed.path)).resolve(strict=False)


def git_origin(path: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "config", "--get", "remote.origin.url"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    origin = proc.stdout.strip()
    return origin if proc.returncode == 0 and origin else None


def installed_repo_url() -> str:
    direct = direct_url_metadata()
    if direct.get("vcs_info", {}).get("vcs") == "git" and direct.get("url"):
        return str(direct["url"])

    if direct.get("url"):
        local_path = file_url_to_path(str(direct["url"]))
        if local_path:
            origin = git_origin(local_path)
            if origin:
                return origin

    source_root = Path(__file__).resolve().parents[1]
    origin = git_origin(source_root)
    return origin or DEFAULT_REPO_URL


def github_repo_slug(repo_url: str) -> str | None:
    url = repo_url.removeprefix("git+").removesuffix(".git")
    match = re.search(r"github\.com[:/]([^/]+)/([^/#?]+)$", url)
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def latest_tag_from_github_api(repo_url: str, *, timeout: float) -> str | None:
    slug = github_repo_slug(repo_url)
    if not slug:
        return None
    request = urllib.request.Request(
        f"https://api.github.com/repos/{slug}/tags?per_page=100",
        headers={"Accept": "application/vnd.github+json", "User-Agent": f"{PACKAGE_NAME}-update-check"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    tags = [item.get("name") for item in payload if isinstance(item, dict)]
    return newest_tag(tag for tag in tags if isinstance(tag, str))


def newest_tag(tags: Any) -> str | None:
    parsed: list[tuple[tuple[int, int, int], str]] = []
    for tag in tags:
        version = parse_version(str(tag))
        if version:
            parsed.append((version, str(tag)))
    if not parsed:
        return None
    return max(parsed, key=lambda item: item[0])[1]


def latest_tag_from_git(repo_url: str, *, timeout: float) -> str | None:
    proc = subprocess.run(
        ["git", "ls-remote", "--tags", "--refs", repo_url],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git ls-remote failed")
    tags = []
    for line in proc.stdout.splitlines():
        _sha, _sep, ref = line.partition("\t")
        if ref.startswith("refs/tags/"):
            tags.append(ref.removeprefix("refs/tags/"))
    return newest_tag(tags)


def latest_available_tag(repo_url: str, *, timeout: float = UPDATE_CHECK_TIMEOUT) -> str | None:
    try:
        return latest_tag_from_git(repo_url, timeout=timeout)
    except (FileNotFoundError, RuntimeError, subprocess.TimeoutExpired):
        return latest_tag_from_github_api(repo_url, timeout=timeout)


def load_update_cache() -> dict[str, Any]:
    path = update_cache_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_update_cache(data: dict[str, Any]) -> None:
    try:
        ensure_global()
        update_cache_path().write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    except OSError:
        pass


def get_update_info(*, force: bool = False, timeout: float = UPDATE_CHECK_TIMEOUT) -> dict[str, Any]:
    now = time.time()
    current = current_version()
    cache = load_update_cache()
    try:
        checked_at = float(cache.get("checked_at", 0))
    except (TypeError, ValueError):
        checked_at = 0
    if (
        not force
        and checked_at
        and now - checked_at < UPDATE_CHECK_INTERVAL
        and cache.get("current_version") == current
    ):
        return cache

    repo_url = installed_repo_url()
    info: dict[str, Any] = {"checked_at": now, "current_version": current, "repo_url": repo_url}
    try:
        tag = latest_available_tag(repo_url, timeout=timeout)
        if tag:
            info["latest_tag"] = tag
            info["latest_version"] = tag.lstrip("v")
            info["update_available"] = is_newer_version(tag, str(info["current_version"]))
        else:
            info["error"] = "no version tags found"
            info["update_available"] = False
    except Exception as exc:
        info["error"] = str(exc) or exc.__class__.__name__
        info["update_available"] = False
    save_update_cache(info)
    return info


def pip_git_url(repo_url: str) -> str:
    url = repo_url.removeprefix("git+")
    match = re.fullmatch(r"([^@\s]+@[^:\s]+):(.+)", url)
    if match:
        return f"ssh://{match.group(1)}/{match.group(2)}"
    return url


def git_install_spec(repo_url: str, tag: str | None = None) -> str:
    spec = "git+" + pip_git_url(repo_url)
    if tag:
        spec += f"@{tag}"
    return spec


def pipx_package_name() -> str | None:
    metadata_path = Path(sys.prefix) / "pipx_metadata.json"
    if not metadata_path.exists():
        return None
    try:
        data = json.loads(metadata_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    main_package = data.get("main_package", {}) if isinstance(data, dict) else {}
    package = str(main_package.get("package") or "")
    return package if package == PACKAGE_NAME else None


def update_install_command(repo_url: str, tag: str) -> list[str]:
    spec = git_install_spec(repo_url, tag)
    pipx_package = pipx_package_name()
    if pipx_package and shutil.which("pipx"):
        return ["pipx", "install", "--force", spec]
    return [sys.executable, "-m", "pip", "install", "--upgrade", "--force-reinstall", spec]


def maybe_print_update_notice() -> None:
    info = get_update_info(force=False, timeout=UPDATE_CHECK_TIMEOUT)
    if not info.get("update_available"):
        return
    current = info.get("current_version", "unknown")
    latest = info.get("latest_tag") or info.get("latest_version")
    console.print(
        f"[yellow]Hey, there is a newer aim version available ({current} → {latest}). "
        "Do an `aim update` please.[/]"
    )


def ensure_project(root: Path, *, force: bool = False) -> None:
    pdir = project_aim_dir(root)
    pdir.mkdir(exist_ok=True)
    df = dockerfile_path(root)
    cfg = project_config_path(root)
    if force or not df.exists():
        df.write_text(DOCKERFILE)
        console.print(f"[green]{'updated' if force else 'created'}[/] {df}")
    else:
        text = df.read_text()
        updated = text
        messages: list[str] = []
        if LEGACY_UID_BLOCK in updated:
            updated = updated.replace(LEGACY_UID_BLOCK, FIXED_UID_BLOCK)
            messages.append("UID handling")
        if "AIM_TOOLS_REFRESH" not in updated and LEGACY_TOOLS_BLOCK in updated:
            updated = updated.replace(LEGACY_TOOLS_BLOCK, TOOLS_REFRESH_BLOCK)
            messages.append("tool refresh cache-busting")
        if updated != text:
            df.write_text(updated)
            console.print(f"[green]updated[/] {df} {', '.join(messages)}")
    if not cfg.exists():
        cfg.write_text(PROJECT_CONFIG)
        console.print(f"[green]created[/] {cfg}")


def load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def toml_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{k} = {toml_value(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"cannot write TOML value: {value!r}")


def write_toml(path: Path, data: dict[str, Any], header: str = "") -> None:
    lines: list[str] = []
    if header:
        lines.append(header.rstrip())
        lines.append("")
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"[{key}]")
            for k, v in value.items():
                lines.append(f"{k} = {toml_value(v)}")
            lines.append("")
        else:
            lines.append(f"{key} = {toml_value(value)}")
    path.write_text("\n".join(lines).rstrip() + "\n")


def save_project(root: Path, data: dict[str, Any]) -> None:
    data.setdefault("config_version", CONFIG_VERSION)
    write_toml(project_config_path(root), data, "# Project-local aim config. Safe to commit/share.")


def save_global(data: dict[str, Any]) -> None:
    ensure_global()
    write_toml(global_config_path(), data, "# User-local aim config. Reserved for future personal defaults.")


def run_process(args: list[str], *, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {"text": True}
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc = subprocess.run(args, **kwargs)
    if check and proc.returncode != 0:
        if capture and proc.stderr:
            console.print(proc.stderr.rstrip(), style="red")
        raise typer.Exit(proc.returncode)
    return proc


def docker(args: list[str], *, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return run_process(["docker", *args], capture=capture, check=check)
    except FileNotFoundError:
        console.print("[red]docker binary not found[/]")
        raise typer.Exit(1)


def docker_ok() -> bool:
    try:
        return run_process(["docker", "info"], capture=True, check=False).returncode == 0
    except FileNotFoundError:
        return False


def slug(root: Path) -> str:
    base = re.sub(r"[^a-zA-Z0-9_.-]+", "-", root.name).strip("-._").lower() or "project"
    h = hashlib.sha1(str(root).encode()).hexdigest()[:10]
    return f"{base}-{h}"


def image_name(root: Path) -> str:
    return f"aim-{slug(root)}:latest"


def container_name(root: Path) -> str:
    user = re.sub(r"[^a-zA-Z0-9_.-]+", "-", getpass.getuser()).lower() or "user"
    return f"aim-{user}-{slug(root)}"


def image_exists(image: str) -> bool:
    return docker(["image", "inspect", image], capture=True, check=False).returncode == 0


def image_label(image: str, label: str) -> str:
    tmpl = "{{ index .Config.Labels " + json.dumps(label) + " }}"
    out = docker(["image", "inspect", "-f", tmpl, image], capture=True, check=False)
    return out.stdout.strip() if out.returncode == 0 else ""


def container_exists(name: str) -> bool:
    return docker(["container", "inspect", name], capture=True, check=False).returncode == 0


def container_running(name: str) -> bool:
    out = docker(["inspect", "-f", "{{.State.Running}}", name], capture=True, check=False)
    return out.returncode == 0 and out.stdout.strip() == "true"


def container_label(name: str, label: str) -> str:
    tmpl = "{{ index .Config.Labels " + json.dumps(label) + " }}"
    out = docker(["inspect", "-f", tmpl, name], capture=True, check=False)
    return out.stdout.strip() if out.returncode == 0 else ""


def host_ids() -> tuple[int, int]:
    return (os.getuid() if hasattr(os, "getuid") else 1000, os.getgid() if hasattr(os, "getgid") else 1000)


def user_spec(config: dict[str, Any]) -> tuple[str, int, int]:
    container = config.get("container", {})
    uid, gid = host_ids()
    username = container.get("username") or getpass.getuser() or "aim"
    username = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(username)).strip("-") or "aim"
    return username, int(container.get("uid", uid)), int(container.get("gid", gid))


def build_hash(root: Path, username: str, uid: int, gid: int) -> str:
    payload = {
        "dockerfile": dockerfile_path(root).read_text(),
        "username": username,
        "uid": uid,
        "gid": gid,
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def docker_build_args(
    root: Path,
    username: str,
    uid: int,
    gid: int,
    desired: str,
    *,
    no_cache: bool = False,
    tools_refresh: str | None = None,
) -> list[str]:
    args = [
        "build",
        "-t", image_name(root),
        "--label", "aim.managed=1",
        "--label", f"aim.build={desired}",
    ]
    if no_cache:
        args.append("--no-cache")
    args += [
        "-f", str(dockerfile_path(root)),
        "--build-arg", f"USERNAME={username}",
        "--build-arg", f"UID={uid}",
        "--build-arg", f"GID={gid}",
    ]
    if tools_refresh is not None:
        args += ["--build-arg", f"AIM_TOOLS_REFRESH={tools_refresh}"]
    args.append(str(project_aim_dir(root)))
    return args


def dockerfile_supports_tools_refresh(root: Path) -> bool:
    text = dockerfile_path(root).read_text()
    return "ARG AIM_TOOLS_REFRESH" in text and ("${AIM_TOOLS_REFRESH}" in text or "$AIM_TOOLS_REFRESH" in text)


def build_image(
    root: Path,
    *,
    force: bool = False,
    no_cache: bool = False,
    tools_refresh: str | None = None,
) -> None:
    ensure_project(root)
    cfg = load_toml(project_config_path(root))
    username, uid, gid = user_spec(cfg)
    image = image_name(root)
    desired = build_hash(root, username, uid, gid)
    if not force and not no_cache and tools_refresh is None and image_exists(image) and image_label(image, "aim.build") == desired:
        return
    console.print(f"[cyan]building[/] {image}")
    docker(docker_build_args(root, username, uid, gid, desired, no_cache=no_cache, tools_refresh=tools_refresh))


def network_args(config: dict[str, Any]) -> list[str]:
    net = config.get("network", {})
    mode = str(net.get("mode", "auto"))
    if mode == "off":
        return ["--network", "none"]
    if mode == "host" or (mode == "auto" and platform.system() == "Linux"):
        return ["--network", "host"]
    args: list[str] = []
    for port in net.get("ports", DEFAULT_PORTS):
        p = int(port)
        args += ["-p", f"127.0.0.1:{p}:{p}"]
    return args


def mount_arg(source: Path | str, target: str, *, readonly: bool = False, kind: str = "bind") -> list[str]:
    if kind == "tmpfs":
        return ["--mount", f"type=tmpfs,target={target},tmpfs-size=1048576"]
    spec = f"type=bind,source={source},target={target}"
    if readonly:
        spec += ",readonly"
    return ["--mount", spec]


def container_path(path: str, username: str) -> str:
    if path == "~":
        return f"/home/{username}"
    if path.startswith("~/"):
        return f"/home/{username}/{path[2:]}"
    return path


def clean_project_paths(paths: list[Any]) -> list[str]:
    seen: set[str] = set()
    clean: list[str] = []
    for p in paths:
        p = str(p).strip().strip("/")
        if not p or p in seen or Path(p).is_absolute() or ".." in Path(p).parts:
            continue
        if any(ch in p for ch in "*?["):
            continue
        seen.add(p)
        clean.append(p)
    return clean


def clean_share_paths(paths: list[Any]) -> list[str]:
    seen: set[str] = set()
    clean: list[str] = []
    for p in paths:
        p = str(p).strip()
        if not p:
            continue
        if any(ch in p for ch in "*?["):
            continue
        if not (p == "~" or p.startswith("~/") or Path(p).is_absolute()):
            p = p.strip("/")
            if ".." in Path(p).parts:
                continue
        if p not in seen:
            seen.add(p)
            clean.append(p)
    return clean


def complete_agent_names(incomplete: str = "") -> list[str]:
    return [name for name in sorted(AGENT_DIRS) if name.startswith(incomplete)]


def share_config(config: dict[str, Any]) -> dict[str, Any]:
    share = config.setdefault("share", {})
    share.setdefault("agents", [])
    share.setdefault("ssh", {"enabled": False, "host": False, "readonly": False})
    share.setdefault("readonly", list(DEFAULT_READONLY))
    share.setdefault("readwrite", [])
    share.setdefault("hidden", list(DEFAULT_HIDDEN))
    return share


def hidden_paths(root: Path, config: dict[str, Any]) -> list[str]:
    paths = list(share_config(config).get("hidden", DEFAULT_HIDDEN))
    aimignore = root / ".aimignore"
    if aimignore.exists():
        for line in aimignore.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                paths.append(line)
    return clean_share_paths(paths)


def readonly_paths(config: dict[str, Any]) -> list[str]:
    return clean_share_paths(list(share_config(config).get("readonly", DEFAULT_READONLY)))


def readwrite_paths(config: dict[str, Any]) -> list[str]:
    return clean_share_paths(list(share_config(config).get("readwrite", [])))


def share_source_target(root: Path, path: str, username: str) -> tuple[Path, str]:
    if path == "~" or path.startswith("~/"):
        return Path(path).expanduser().resolve(), container_path(path, username)
    if Path(path).is_absolute():
        source = Path(path).expanduser().resolve()
        return source, str(source)
    source = (root / path).resolve()
    return source, str(root / path)


def config_share_path(path: str) -> str | None:
    path = str(path).strip()
    if not path or any(ch in path for ch in "*?["):
        return None
    if path == "~" or path.startswith("~/"):
        return path
    if Path(path).expanduser().is_absolute():
        return str(Path(path).expanduser().resolve())
    path = path.strip("/")
    if not path or ".." in Path(path).parts:
        return None
    return path


def add_shared_path(share: dict[str, Any], path: str, *, readonly: bool) -> None:
    key = "readonly" if readonly else "readwrite"
    other = "readwrite" if readonly else "readonly"
    share[other] = [p for p in clean_share_paths(list(share.get(other, []))) if p != path]
    paths = [p for p in clean_share_paths(list(share.get(key, []))) if p != path]
    paths.append(path)
    share[key] = paths


def remove_shared_path(share: dict[str, Any], path: str) -> bool:
    removed = False
    for key in ("readonly", "readwrite"):
        before = clean_share_paths(list(share.get(key, [])))
        after = [p for p in before if p != path]
        share[key] = after
        removed = removed or len(after) != len(before)
    return removed


def base_share_mounts(config: dict[str, Any], username: str) -> list[tuple[Path, str, bool]]:
    share = share_config(config)
    mounts: list[tuple[Path, str, bool]] = []
    home = f"/home/{username}"

    ssh = share.get("ssh", {})
    if isinstance(ssh, dict) and ssh.get("enabled"):
        src = Path.home() / ".ssh" if ssh.get("host") else GLOBAL_DIR / "share" / "ssh"
        src.mkdir(parents=True, exist_ok=True)
        mounts.append((src, f"{home}/.ssh", bool(ssh.get("readonly", False))))

    for item in share.get("agents", []):
        if isinstance(item, str):
            item = {"name": item, "host": False}
        if not isinstance(item, dict) or "name" not in item:
            continue
        name = str(item["name"])
        target_dir = AGENT_DIRS.get(name, f".{name}")
        src = Path.home() / target_dir if item.get("host") else GLOBAL_DIR / "share" / "agents" / name
        src.mkdir(parents=True, exist_ok=True)
        mounts.append((src, f"{home}/{target_dir}", False))

    return mounts


def target_depth(target: str) -> int:
    return len(Path(target).parts)


def mounted_targets(root: Path, config: dict[str, Any], username: str) -> list[str]:
    targets = [str(root)]
    targets += [target for _src, target, _ro in base_share_mounts(config, username)]
    for p in readonly_paths(config) + readwrite_paths(config):
        src, target = share_source_target(root, p, username)
        if src.exists():
            targets.append(target)
    return targets


def all_mounts(root: Path, config: dict[str, Any], username: str) -> list[str]:
    args: list[str] = []
    args += mount_arg(root, str(root))

    for src, target, readonly in sorted(base_share_mounts(config, username), key=lambda item: target_depth(item[1])):
        args += mount_arg(src, target, readonly=readonly)

    mounted = mounted_targets(root, config, username)
    rules: list[tuple[str, int, Path | str, str, bool]] = []

    for p in readonly_paths(config):
        src, target = share_source_target(root, p, username)
        if not src.exists():
            console.print(f"[yellow]skipping missing shared path[/] {src}")
            continue
        rules.append((target, 0, src, target, True))

    for p in hidden_paths(root, config):
        src, target = share_source_target(root, p, username)
        if not any(path_is_under(target, mounted_target) for mounted_target in mounted):
            continue
        if src.is_dir():
            rules.append((target, 1, "", target, False))
        elif src.is_file():
            rules.append((target, 1, "/dev/null", target, True))

    for p in readwrite_paths(config):
        src, target = share_source_target(root, p, username)
        if not src.exists():
            console.print(f"[yellow]skipping missing shared path[/] {src}")
            continue
        rules.append((target, 2, src, target, False))

    for _target, _precedence, src, target, readonly in sorted(rules, key=lambda item: (target_depth(item[0]), item[1])):
        if src == "":
            args += mount_arg(src, target, kind="tmpfs")
        else:
            args += mount_arg(src, target, readonly=readonly)

    return args


def configured_mount_targets(root: Path, config: dict[str, Any], username: str) -> list[str]:
    targets = mounted_targets(root, config, username)
    mounted = list(targets)
    for p in hidden_paths(root, config):
        src, target = share_source_target(root, p, username)
        if src.exists() and any(path_is_under(target, mounted_target) for mounted_target in mounted):
            targets.append(target)

    seen: set[str] = set()
    clean: list[str] = []
    for target in targets:
        target = "/" + target.strip("/") if target != "/" else "/"
        if target not in seen:
            seen.add(target)
            clean.append(target)
    return clean


def container_mount_targets(name: str) -> list[str]:
    out = docker(["inspect", "-f", "{{json .Mounts}}", name], capture=True, check=False)
    if out.returncode != 0 or not out.stdout.strip():
        return []
    try:
        mounts = json.loads(out.stdout)
    except json.JSONDecodeError:
        return []
    targets: list[str] = []
    for mount in mounts:
        if isinstance(mount, dict) and mount.get("Destination"):
            targets.append(str(mount["Destination"]))
    return targets


def path_is_under(path: str, parent: str) -> bool:
    parent = parent.rstrip("/") or "/"
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def parse_docker_diff(output: str) -> list[tuple[str, str]]:
    changes: list[tuple[str, str]] = []
    for line in output.splitlines():
        line = line.strip()
        if len(line) < 3 or line[1] != " ":
            continue
        status, path = line[0], line[2:]
        if status in {"A", "C", "D"} and path.startswith("/"):
            changes.append((status, path))
    return changes


def container_system_changes(root: Path, config: dict[str, Any], username: str, name: str) -> list[tuple[str, str]]:
    if not container_exists(name):
        return []
    out = docker(["diff", name], capture=True, check=False)
    if out.returncode != 0:
        return []
    mount_targets = configured_mount_targets(root, config, username) + container_mount_targets(name)
    changes = []
    for status, path in parse_docker_diff(out.stdout):
        if any(path_is_under(path, target) for target in mount_targets):
            continue
        if any(path_is_under(path, ignored) for ignored in DOCKER_MANAGED_PATHS):
            continue
        if any(path_is_under(path, prefix) for prefix in SYSTEM_CHANGE_PREFIXES):
            changes.append((status, path))

    # docker diff can include changed parent directories. For common system
    # parent directories, keep those only when there is a concrete child change
    # after filtering Docker-managed files.
    return [
        (status, path)
        for status, path in changes
        if status != "C"
        or path not in NOISY_PARENT_CHANGE_PATHS
        or any(other != path and path_is_under(other, path) for _other_status, other in changes)
    ]


def desired_hash(root: Path, config: dict[str, Any], username: str) -> str:
    payload = {
        "root": str(root),
        "image": image_name(root),
        "user": username,
        "network": network_args(config),
        "mounts": all_mounts(root, config, username),
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def remove_container(name: str) -> None:
    if container_exists(name):
        docker(["rm", "-f", name], check=False)


def remove_all_docker_artifacts() -> None:
    containers = docker(["ps", "-aq", "--filter", "label=aim.managed=1"], capture=True, check=False).stdout.split()
    if containers:
        docker(["rm", "-f", *containers], check=False)

    out = docker(["images", "--format", "{{.Repository}}:{{.Tag}}"], capture=True, check=False)
    images = [line for line in out.stdout.splitlines() if line.startswith("aim-")]
    if images:
        docker(["rmi", *images], check=False)


def ensure_container(root: Path) -> None:
    ensure_global()
    ensure_project(root)
    build_image(root)
    cfg = load_toml(project_config_path(root))
    username, _uid, _gid = user_spec(cfg)
    name = container_name(root)
    desired = desired_hash(root, cfg, username)

    if container_exists(name):
        current = container_label(name, "aim.config")
        if current != desired:
            console.print("[yellow]workspace config changed; recreating container[/]")
            remove_container(name)
        elif not container_running(name):
            docker(["start", name])
            return
        else:
            return

    home = f"/home/{username}"
    args = [
        "run", "-d",
        "--name", name,
        "--hostname", "aim",
        "--add-host", "aim:127.0.0.1",
        "--label", "aim.managed=1",
        "--label", f"aim.project={root}",
        "--label", f"aim.config={desired}",
        "--user", username,
        "-e", f"HOME={home}",
        "-e", "TERM=xterm-256color",
        "-w", str(root),
        *network_args(cfg),
        *all_mounts(root, cfg, username),
        image_name(root),
        "sleep", "infinity",
    ]
    console.print(f"[cyan]starting[/] {name}")
    docker(args)


def docker_exec(root: Path, command: list[str]) -> None:
    cfg = load_toml(project_config_path(root))
    username, _uid, _gid = user_spec(cfg)
    args = ["docker", "exec"]
    args.append("-it" if sys.stdin.isatty() and sys.stdout.isatty() else "-i")
    args += [
        "--user", username,
        "-e", f"HOME=/home/{username}",
        "-e", "TERM=xterm-256color",
        "-w", str(root),
        container_name(root),
        *command,
    ]
    raise typer.Exit(subprocess.call(args))


def enter_shell(root: Path) -> None:
    docker_exec(root, ["bash", "-l"])


def launch() -> None:
    maybe_print_update_notice()
    if not docker_ok():
        console.print("[red]Docker is not available or the daemon is not running.[/]")
        raise typer.Exit(1)
    root = project_root()
    ensure_container(root)
    enter_shell(root)


def print_version(value: bool) -> None:
    if value:
        console.print(current_version())
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False,
        "--version",
        callback=print_version,
        is_eager=True,
        help="Show the aim version and exit.",
    ),
) -> None:
    if ctx.invoked_subcommand is None:
        launch()


@app.command()
def init(force: bool = typer.Option(False, "--force", help="Overwrite .aim/Dockerfile with the current default.")) -> None:
    """Create .aim/Dockerfile and .aim/config.toml if missing."""
    ensure_global()
    ensure_project(project_root(), force=force)
    console.print("[green]aim initialized[/]")


@app.command()
def update(
    check: bool = typer.Option(False, "--check", help="Only check whether an update is available."),
) -> None:
    """Check for and install a newer aim release from GitHub."""
    info = get_update_info(force=True, timeout=10.0)
    current = str(info.get("current_version", "unknown"))
    repo_url = str(info.get("repo_url", DEFAULT_REPO_URL))

    if info.get("error"):
        console.print(f"[red]could not check for updates:[/] {escape(str(info['error']))}")
        raise typer.Exit(1)

    latest_tag = str(info.get("latest_tag") or "")
    if not latest_tag:
        console.print("[yellow]no version tags found[/]")
        raise typer.Exit(1)

    if not info.get("update_available"):
        console.print(f"[green]aim is up to date[/] ({current}, latest {latest_tag})")
        return

    console.print(f"[yellow]new aim version available:[/] {current} → {latest_tag}")
    if check:
        raise typer.Exit(1)

    command = update_install_command(repo_url, latest_tag)
    console.print("[cyan]update command:[/] " + " ".join(shlex.quote(arg) for arg in command))
    run_process(command)
    console.print("[green]aim updated[/] Restart `aim` to use the new version.")


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def run(ctx: typer.Context) -> None:
    """Ensure the workspace is running, then execute a command inside it."""
    command = list(ctx.args)
    if not command:
        console.print("[red]usage:[/] aim run COMMAND [ARGS]...")
        raise typer.Exit(2)
    if not docker_ok():
        console.print("[red]Docker is not available or the daemon is not running.[/]")
        raise typer.Exit(1)
    root = project_root()
    ensure_container(root)
    docker_exec(root, command)


def print_container_update_warning(changes: list[tuple[str, str]], *, limit: int = 25) -> None:
    console.print("[yellow]This container appears to have manual system changes that may be lost.[/]")
    console.print("\nDetected changes:")
    for status, path in changes[:limit]:
        console.print(f"  {status} {escape(path)}")
    if len(changes) > limit:
        console.print(f"  ... and {len(changes) - limit} more")

    console.print(
        "\nIf you installed apt packages manually inside the container, add them to "
        "[bold].aim/Dockerfile[/] instead, for example:\n"
    )
    console.print("  RUN apt-get update \\")
    console.print("      && apt-get install -y --no-install-recommends htop jq \\")
    console.print("      && rm -rf /var/lib/apt/lists/*")
    console.print(
        "\nTo inspect apt history before updating, abort and run:\n"
        "  aim run -- sh -lc 'cat /var/log/apt/history.log'\n"
    )


@app.command()
def rebuild(no_cache: bool = typer.Option(False, "--no-cache", help="Disable Docker build cache.")) -> None:
    """Rebuild the project image and recreate the workspace container."""
    root = project_root()
    ensure_global()
    ensure_project(root)
    build_image(root, force=True, no_cache=no_cache)
    remove_container(container_name(root))
    console.print("[green]rebuilt; next `aim` will start a fresh container[/]")


@app.command(name="update-container")
def update_container(
    discard: bool = typer.Option(False, "--discard", "-y", help="Do not prompt before discarding container-local system changes."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Disable Docker build cache for the whole image."),
) -> None:
    """Refresh preinstalled tools by rebuilding the image and replacing the container."""
    if not docker_ok():
        console.print("[red]Docker is not available or the daemon is not running.[/]")
        raise typer.Exit(1)

    root = project_root()
    ensure_global()
    ensure_project(root)
    cfg = load_toml(project_config_path(root))
    username, _uid, _gid = user_spec(cfg)
    name = container_name(root)

    changes = container_system_changes(root, cfg, username, name)
    if changes:
        print_container_update_warning(changes)
        if not discard and not typer.confirm("Continue and discard container-local system changes?", default=False):
            console.print("[yellow]aborted[/]")
            raise typer.Exit(1)

    supports_refresh = dockerfile_supports_tools_refresh(root)
    if not supports_refresh and not no_cache:
        console.print("[yellow]Dockerfile has no AIM_TOOLS_REFRESH cache-bust hook; using --no-cache.[/]")

    refresh = str(int(time.time())) if supports_refresh and not no_cache else None
    build_image(root, force=True, no_cache=no_cache or not supports_refresh, tools_refresh=refresh)
    remove_container(name)
    console.print("[green]updated container image; next `aim` will start a fresh container[/]")


@app.command()
def reset() -> None:
    """Destroy and recreate the workspace, then enter it."""
    root = project_root()
    remove_container(container_name(root))
    ensure_container(root)
    enter_shell(root)


@app.command()
def clean(
    image: bool = typer.Option(False, "--image", help="Also remove the current project's Docker image."),
    all_: bool = typer.Option(False, "--all", help="Remove all aim-managed Docker containers and aim images."),
    force: bool = typer.Option(False, "--force", "-f", help="Do not prompt when using --all."),
) -> None:
    """Remove workspace Docker artifacts."""
    if all_:
        if not force and not typer.confirm("Remove all aim containers and aim images?"):
            raise typer.Exit(1)
        remove_all_docker_artifacts()
        console.print("[green]cleaned all aim Docker artifacts[/]")
        return

    root = project_root()
    remove_container(container_name(root))
    if image:
        docker(["rmi", image_name(root)], check=False)
    console.print("[green]cleaned[/]")


@app.command(name="list")
def list_workspaces() -> None:
    """List aim-managed containers."""
    docker(["ps", "-a", "--filter", "label=aim.managed=1", "--format", "table {{.Names}}\t{{.Status}}\t{{.Image}}"])


def symlink_target(path: Path) -> Path:
    raw = Path(os.readlink(path))
    if not raw.is_absolute():
        raw = path.parent / raw
    return raw.resolve(strict=False)


def outside_symlinks(root: Path, *, limit: int = 20) -> list[tuple[Path, Path]]:
    found: list[tuple[Path, Path]] = []
    try:
        paths = root.rglob("*")
        for path in paths:
            if len(found) >= limit:
                break
            try:
                if not path.is_symlink():
                    continue
                target = symlink_target(path)
                if not target.is_relative_to(root):
                    found.append((path, target))
            except OSError:
                continue
    except OSError:
        pass
    return found


def doctor_share_warnings(config: dict[str, Any]) -> list[str]:
    share = share_config(config)
    warnings: list[str] = []
    for item in share.get("agents", []):
        if isinstance(item, str):
            continue
        if isinstance(item, dict) and item.get("host"):
            warnings.append(f"agent {item.get('name')} uses the real host directory")
    ssh = share.get("ssh", {})
    if isinstance(ssh, dict) and ssh.get("enabled"):
        warnings.append("ssh is shared with the container" + (" from real ~/.ssh" if ssh.get("host") else ""))
    for path in readwrite_paths(config):
        warnings.append(f"path is shared read-write: {path}")
    return warnings


@app.command()
def doctor() -> None:
    """Check Docker and local aim configuration."""
    ok = True
    docker_bin = shutil.which("docker")
    if docker_bin:
        console.print(f"[green]docker binary[/] {docker_bin}")
    else:
        console.print("[red]docker binary not found[/]")
        ok = False
    if docker_ok():
        console.print("[green]docker daemon[/] running")
    else:
        console.print("[red]docker daemon not reachable[/]")
        ok = False
    root = project_root()
    cfg = load_toml(project_config_path(root))
    console.print(f"project:        {root}")
    console.print(f"project config: {project_config_path(root)}")
    console.print(f"user storage:   {GLOBAL_DIR / 'share'}")

    outside = outside_symlinks(root)
    if outside:
        console.print("[yellow]outside-project symlinks:[/]")
        for path, target in outside:
            console.print(f"  {path.relative_to(root)} -> {target}")

    warnings = doctor_share_warnings(cfg)
    if warnings:
        console.print("[yellow]share warnings:[/]")
        for warning in warnings:
            console.print(f"  {warning}")

    net_mode = str(cfg.get("network", {}).get("mode", "auto"))
    if net_mode in {"auto", "host"} and platform.system() == "Linux":
        console.print("[dim]network: host networking; use `aim network bridge` for more isolation.[/]")

    raise typer.Exit(0 if ok else 1)


def print_shares(path: Path, cfg: dict[str, Any]) -> None:
    share = share_config(cfg)
    console.print(f"config: [bold]{path}[/]")

    agents = share.get("agents", [])
    console.print("\n[bold]agents[/]")
    if agents:
        for item in agents:
            if isinstance(item, str):
                name, host = item, False
            else:
                name, host = str(item.get("name")), bool(item.get("host", False))
            target_dir = AGENT_DIRS.get(name, f".{name}")
            src = Path.home() / target_dir if host else GLOBAL_DIR / "share" / "agents" / name
            console.print(f"  {name}: {src} -> ~/{target_dir}{' [host]' if host else ''}")
    else:
        console.print("  none")

    ssh = share.get("ssh", {})
    console.print("\n[bold]ssh[/]")
    if isinstance(ssh, dict) and ssh.get("enabled"):
        src = Path.home() / ".ssh" if ssh.get("host") else GLOBAL_DIR / "share" / "ssh"
        console.print(f"  {src} -> ~/.ssh ({'ro' if ssh.get('readonly') else 'rw'}{' host' if ssh.get('host') else ''})")
    else:
        console.print("  none")

    console.print("\n[bold]readonly[/]")
    readonly = readonly_paths(cfg)
    if readonly:
        for path in readonly:
            console.print(f"  {path}")
    else:
        console.print("  none")

    console.print("\n[bold]readwrite[/]")
    readwrite = readwrite_paths(cfg)
    if readwrite:
        for path in readwrite:
            console.print(f"  {path}")
    else:
        console.print("  none")

    console.print("\n[bold]hidden[/]")
    hidden = hidden_paths(path.parent.parent, cfg) if path.name == "config.toml" else clean_project_paths(list(share.get("hidden", [])))
    if hidden:
        for item in hidden:
            console.print(f"  {item}")
    else:
        console.print("  none")


@share_app.command("list")
def share_list() -> None:
    """List currently shared resources for this project."""
    root = project_root()
    ensure_project(root)
    print_shares(project_config_path(root), load_toml(project_config_path(root)))


@share_app.command("agent")
def share_agent(
    name: str = typer.Argument(..., autocompletion=complete_agent_names),
    host: bool = typer.Option(False, "--host/--managed", help="Use the real host agent directory instead of ~/.aim/share/agents/NAME."),
) -> None:
    """Share persistent state/config for an AI CLI in this project."""
    ensure_global()
    root = project_root()
    ensure_project(root)
    cfg = load_toml(project_config_path(root))
    share = share_config(cfg)
    agents = [a for a in share.get("agents", []) if not (isinstance(a, dict) and a.get("name") == name) and a != name]
    agents.append({"name": name, "host": bool(host)})
    share["agents"] = agents
    target_dir = AGENT_DIRS.get(name, f".{name}")
    src = Path.home() / target_dir if host else GLOBAL_DIR / "share" / "agents" / name
    src.mkdir(parents=True, exist_ok=True)
    save_project(root, cfg)
    console.print(f"[green]shared agent[/] {name}: {src} -> ~/{target_dir}")


@share_app.command("ssh")
def share_ssh(
    host: bool = typer.Option(False, "--host/--managed", help="Use real ~/.ssh instead of ~/.aim/share/ssh."),
    ro: bool = typer.Option(False, "--ro/--rw", help="Mount SSH read-only/read-write."),
) -> None:
    """Share SSH identity/config as ~/.ssh inside this project's container."""
    ensure_global()
    root = project_root()
    ensure_project(root)
    cfg = load_toml(project_config_path(root))
    share = share_config(cfg)
    share["ssh"] = {"enabled": True, "host": bool(host), "readonly": bool(ro)}
    src = Path.home() / ".ssh" if host else GLOBAL_DIR / "share" / "ssh"
    src.mkdir(parents=True, exist_ok=True)
    save_project(root, cfg)
    console.print(f"[green]shared ssh[/] {src} -> ~/.ssh ({'ro' if ro else 'rw'})")


@share_app.command("dir")
def share_dir(
    path: str,
    ro: bool = typer.Option(False, "--ro/--rw", help="Mount read-only/read-write."),
) -> None:
    """Share a directory path with this project's container."""
    ensure_global()
    root = project_root()
    ensure_project(root)
    item = config_share_path(path)
    if item is None:
        console.print(f"[red]invalid path:[/] {path}")
        raise typer.Exit(1)
    src, target = share_source_target(root, item, getpass.getuser())
    if not src.is_dir():
        console.print(f"[red]not a directory:[/] {src}")
        raise typer.Exit(1)
    cfg = load_toml(project_config_path(root))
    share = share_config(cfg)
    add_shared_path(share, item, readonly=bool(ro))
    save_project(root, cfg)
    console.print(f"[green]shared dir[/] {item} -> {target} ({'ro' if ro else 'rw'})")


@share_app.command("file")
def share_file(
    path: str,
    ro: bool = typer.Option(False, "--ro/--rw", help="Mount read-only/read-write."),
) -> None:
    """Share a file path with this project's container."""
    ensure_global()
    root = project_root()
    ensure_project(root)
    item = config_share_path(path)
    if item is None:
        console.print(f"[red]invalid path:[/] {path}")
        raise typer.Exit(1)
    src, target = share_source_target(root, item, getpass.getuser())
    if not src.is_file():
        console.print(f"[red]not a file:[/] {src}")
        raise typer.Exit(1)
    cfg = load_toml(project_config_path(root))
    share = share_config(cfg)
    add_shared_path(share, item, readonly=bool(ro))
    save_project(root, cfg)
    console.print(f"[green]shared file[/] {item} -> {target} ({'ro' if ro else 'rw'})")


@unshare_app.command("agent")
def unshare_agent(name: str = typer.Argument(..., autocompletion=complete_agent_names)) -> None:
    """Stop sharing persistent state/config for an AI CLI in this project."""
    root = project_root()
    ensure_project(root)
    cfg = load_toml(project_config_path(root))
    share = share_config(cfg)
    before = len(share.get("agents", []))
    share["agents"] = [a for a in share.get("agents", []) if not (a == name or (isinstance(a, dict) and a.get("name") == name))]
    save_project(root, cfg)
    console.print(f"[green]unshared agent[/] {name}" if len(share["agents"]) != before else f"[yellow]agent was not shared[/] {name}")


@unshare_app.command("ssh")
def unshare_ssh() -> None:
    """Stop sharing SSH identity/config in this project."""
    root = project_root()
    ensure_project(root)
    cfg = load_toml(project_config_path(root))
    share = share_config(cfg)
    share["ssh"] = {"enabled": False, "host": False, "readonly": False}
    save_project(root, cfg)
    console.print("[green]unshared ssh[/]")


@unshare_app.command("dir")
def unshare_dir(path: str) -> None:
    """Stop sharing a directory path in this project."""
    root = project_root()
    ensure_project(root)
    item = config_share_path(path)
    if item is None:
        console.print(f"[red]invalid path:[/] {path}")
        raise typer.Exit(1)
    cfg = load_toml(project_config_path(root))
    share = share_config(cfg)
    removed = remove_shared_path(share, item)
    save_project(root, cfg)
    console.print(f"[green]unshared dir[/] {item}" if removed else f"[yellow]dir was not shared[/] {item}")


@unshare_app.command("file")
def unshare_file(path: str) -> None:
    """Stop sharing a file path in this project."""
    root = project_root()
    ensure_project(root)
    item = config_share_path(path)
    if item is None:
        console.print(f"[red]invalid path:[/] {path}")
        raise typer.Exit(1)
    cfg = load_toml(project_config_path(root))
    share = share_config(cfg)
    removed = remove_shared_path(share, item)
    save_project(root, cfg)
    console.print(f"[green]unshared file[/] {item}" if removed else f"[yellow]file was not shared[/] {item}")


@expose_app.command("port")
def expose_port(port: int) -> None:
    """Expose a development port when not using host networking."""
    root = project_root()
    ensure_project(root)
    cfg = load_toml(project_config_path(root))
    net = cfg.setdefault("network", {})
    ports = [int(p) for p in net.get("ports", DEFAULT_PORTS)]
    if port not in ports:
        ports.append(port)
    net["ports"] = sorted(ports)
    save_project(root, cfg)
    console.print(f"[green]exposed port[/] {port}")


@network_app.command("off")
def network_off() -> None:
    """Disable container networking."""
    root = project_root()
    ensure_project(root)
    cfg = load_toml(project_config_path(root))
    cfg.setdefault("network", {})["mode"] = "off"
    save_project(root, cfg)
    console.print("[green]network[/] off")


@network_app.command("host")
def network_host() -> None:
    """Use Docker host networking."""
    root = project_root()
    ensure_project(root)
    cfg = load_toml(project_config_path(root))
    cfg.setdefault("network", {})["mode"] = "host"
    save_project(root, cfg)
    console.print("[green]network[/] host")


@network_app.command("bridge")
def network_bridge() -> None:
    """Use Docker bridge networking with configured port forwards."""
    root = project_root()
    ensure_project(root)
    cfg = load_toml(project_config_path(root))
    cfg.setdefault("network", {})["mode"] = "bridge"
    save_project(root, cfg)
    console.print("[green]network[/] bridge")


@network_app.command("auto")
def network_auto() -> None:
    """Use host networking on Linux, otherwise expose configured ports."""
    root = project_root()
    ensure_project(root)
    cfg = load_toml(project_config_path(root))
    cfg.setdefault("network", {})["mode"] = "auto"
    save_project(root, cfg)
    console.print("[green]network[/] auto")


if __name__ == "__main__":
    app()
