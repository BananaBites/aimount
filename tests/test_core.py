from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import aim.cli as cli


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home = tmp_path / "home"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(cli, "GLOBAL_DIR", home / ".aim")
    monkeypatch.chdir(project)
    return home, project


def mount_specs(args: list[str]) -> list[str]:
    return [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "--mount"]


def test_version_option_prints_current_version() -> None:
    result = CliRunner().invoke(cli.app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == cli.current_version()


def test_status_reports_project_without_docker(workspace: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    _home, project = workspace
    monkeypatch.setattr(cli, "docker_ok", lambda: False)

    result = CliRunner().invoke(cli.app, ["status"])

    assert result.exit_code == 0
    assert str(project) in result.stdout
    assert "docker:         unavailable" in result.stdout


def test_init_creates_project_files_with_agent_tools(workspace: tuple[Path, Path]) -> None:
    _home, project = workspace

    cli.ensure_global()
    cli.ensure_project(project)

    assert (project / ".aim" / "Dockerfile").exists()
    assert (project / ".aim" / "config.toml").exists()

    config = cli.load_toml(project / ".aim" / "config.toml")
    assert config["config_version"] == 1
    assert config["share"]["readonly"] == [".aim"]
    assert config["share"]["readwrite"] == []
    assert config["share"]["hidden"] == [".env", ".env.local"]
    assert config["share"]["agents"] == []

    dockerfile = (project / ".aim" / "Dockerfile").read_text()
    assert "@earendil-works/pi-coding-agent" in dockerfile
    assert "@openai/codex" in dockerfile
    assert "@anthropic-ai/claude-code" in dockerfile
    assert "@google/gemini-cli" in dockerfile
    assert "NPM_CONFIG_PREFIX=/home/${USERNAME}/.npm-global" in dockerfile
    assert "AIM_TOOLS_REFRESH" in dockerfile


def test_complete_agent_names() -> None:
    assert cli.complete_agent_names("") == ["claude", "codex", "gemini", "pi"]
    assert cli.complete_agent_names("c") == ["claude", "codex"]
    assert cli.complete_agent_names("x") == []


def test_update_version_helpers() -> None:
    assert cli.newest_tag(["v0.4.1", "v0.10.0", "not-a-version"]) == "v0.10.0"
    assert cli.is_newer_version("v0.4.2", "0.4.1")
    assert not cli.is_newer_version("v0.4.1", "0.4.1")
    assert cli.git_install_spec("git@github.com:BananaBites/aimount.git", "v0.4.2") == (
        "git+ssh://git@github.com/BananaBites/aimount.git@v0.4.2"
    )


def test_update_info_uses_cache(workspace: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    _home, _project = workspace
    calls = 0

    def fail_if_called(repo_url: str, *, timeout: float) -> str | None:
        nonlocal calls
        calls += 1
        raise AssertionError("network should not be used with fresh cache")

    cli.save_update_cache(
        {
            "checked_at": 9999999999,
            "current_version": cli.current_version(),
            "latest_tag": "v0.5.1",
            "update_available": True,
            "repo_url": "https://github.com/BananaBites/aimount.git",
        }
    )
    monkeypatch.setattr(cli, "latest_available_tag", fail_if_called)

    info = cli.get_update_info(force=False)

    assert info["latest_tag"] == "v0.5.1"
    assert calls == 0


def test_update_install_command_uses_pip_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "pipx_package_name", lambda: None)

    command = cli.update_install_command("https://github.com/BananaBites/aimount.git", "v0.4.2")

    assert command[:4] == [cli.sys.executable, "-m", "pip", "install"]
    assert command[-1] == "git+https://github.com/BananaBites/aimount.git@v0.4.2"


def test_share_agent_is_project_local_and_uses_managed_storage(workspace: tuple[Path, Path]) -> None:
    home, project = workspace

    cli.share_agent("pi", host=False)

    project_config = cli.load_toml(project / ".aim" / "config.toml")
    assert project_config["share"]["agents"] == [{"name": "pi", "host": False}]
    assert (home / ".aim" / "share" / "agents" / "pi").is_dir()

    user_config = (home / ".aim" / "config.toml").read_text()
    assert "Active shares are project-local" in user_config
    assert "agents =" not in user_config


def test_share_agent_can_use_real_host_location(workspace: tuple[Path, Path]) -> None:
    home, project = workspace
    (home / ".pi").mkdir()

    cli.share_agent("pi", host=True)

    config = cli.load_toml(project / ".aim" / "config.toml")
    assert config["share"]["agents"] == [{"name": "pi", "host": True}]


def test_share_dir_uses_unified_readonly_and_readwrite_lists(workspace: tuple[Path, Path]) -> None:
    home, project = workspace
    downloads = home / "Downloads"
    downloads.mkdir()
    skills = home / ".skills"
    skills.mkdir()

    cli.share_dir(str(downloads), ro=True)
    cli.share_dir(".aim", ro=True)
    cli.share_dir(str(skills), ro=False)

    config = cli.load_toml(project / ".aim" / "config.toml")
    assert config["share"]["readonly"] == [str(downloads), ".aim"]
    assert config["share"]["readwrite"] == [str(skills)]

    result = CliRunner().invoke(cli.app, ["share", "list"])
    assert result.exit_code == 0
    assert str(skills) in result.stdout

    cli.unshare_dir(str(downloads))
    config = cli.load_toml(project / ".aim" / "config.toml")
    assert config["share"]["readonly"] == [".aim"]


def test_unshare_removes_project_share(workspace: tuple[Path, Path]) -> None:
    _home, project = workspace

    cli.share_agent("pi", host=False)
    cli.unshare_agent("pi")

    config = cli.load_toml(project / ".aim" / "config.toml")
    assert config["share"]["agents"] == []


def test_mounts_include_hidden_paths_and_project_shares(workspace: tuple[Path, Path]) -> None:
    home, project = workspace
    (project / ".aim").mkdir()
    (project / ".env").write_text("SECRET=yes")
    (home / ".pi").mkdir()
    downloads = home / "Downloads"
    downloads.mkdir()
    secret_dir = downloads / "secret"
    secret_dir.mkdir()
    cache_dir = downloads / "cache"
    cache_dir.mkdir()
    outside_secret = home / "outside-secret"
    outside_secret.mkdir()
    gitconfig = home / ".gitconfig"
    gitconfig.write_text("[user]\n")

    config = {
        "share": {
            "agents": [{"name": "pi", "host": True}],
            "ssh": {"enabled": False, "host": False, "readonly": False},
            "readonly": [".aim", str(downloads), "~/.gitconfig"],
            "readwrite": [str(cache_dir)],
            "hidden": [".env", str(secret_dir), str(outside_secret)],
        },
    }

    specs = mount_specs(cli.all_mounts(project, config, "hannes"))

    assert f"type=bind,source={project},target={project}" in specs
    assert f"type=bind,source={project / '.aim'},target={project / '.aim'},readonly" in specs
    assert f"type=bind,source=/dev/null,target={project / '.env'},readonly" in specs
    assert f"type=bind,source={home / '.pi'},target=/home/hannes/.pi" in specs
    assert f"type=bind,source={downloads},target={downloads},readonly" in specs
    assert f"type=tmpfs,target={secret_dir},tmpfs-size=1048576" in specs
    assert f"type=bind,source={cache_dir},target={cache_dir}" in specs
    assert not any(str(outside_secret) in spec for spec in specs)
    assert specs.index(f"type=bind,source={downloads},target={downloads},readonly") < specs.index(
        f"type=bind,source={cache_dir},target={cache_dir}"
    )
    assert f"type=bind,source={gitconfig},target=/home/hannes/.gitconfig,readonly" in specs


def test_build_image_skips_when_build_hash_matches(
    workspace: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _home, project = workspace
    cli.ensure_project(project)
    config = cli.load_toml(project / ".aim" / "config.toml")
    username, uid, gid = cli.user_spec(config)
    desired = cli.build_hash(project, username, uid, gid)
    calls: list[list[str]] = []

    monkeypatch.setattr(cli, "image_exists", lambda image: True)
    monkeypatch.setattr(cli, "image_label", lambda image, label: desired)
    monkeypatch.setattr(cli, "docker", lambda args, **kwargs: calls.append(args))

    cli.build_image(project)

    assert calls == []


def test_ensure_project_migrates_old_tool_install_block(workspace: tuple[Path, Path]) -> None:
    _home, project = workspace
    cli.ensure_project(project)
    dockerfile = project / ".aim" / "Dockerfile"
    dockerfile.write_text(dockerfile.read_text().replace(
        'ARG AIM_TOOLS_REFRESH=0\nRUN echo "aim tools refresh: ${AIM_TOOLS_REFRESH}" >/tmp/aim-tools-refresh \\\n  && npm install -g \\\n',
        'RUN npm install -g \\\n',
    ))

    cli.ensure_project(project)

    assert "AIM_TOOLS_REFRESH" in dockerfile.read_text()


def test_build_image_rebuilds_when_build_hash_differs(
    workspace: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _home, project = workspace
    calls: list[list[str]] = []

    monkeypatch.setattr(cli, "image_exists", lambda image: True)
    monkeypatch.setattr(cli, "image_label", lambda image, label: "old")
    monkeypatch.setattr(cli, "docker", lambda args, **kwargs: calls.append(args))

    cli.build_image(project)

    build_args = calls[0]
    assert build_args[:2] == ["build", "-t"]
    assert "--label" in build_args
    assert "aim.managed=1" in build_args
    assert any(arg.startswith("aim.build=") for arg in build_args)


def test_build_image_can_refresh_tools_layer(
    workspace: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _home, project = workspace
    calls: list[list[str]] = []

    monkeypatch.setattr(cli, "docker", lambda args, **kwargs: calls.append(args))

    cli.build_image(project, force=True, tools_refresh="123")

    build_args = calls[0]
    assert "--build-arg" in build_args
    assert "AIM_TOOLS_REFRESH=123" in build_args


def test_parse_docker_diff_and_filter_system_changes(
    workspace: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _home, project = workspace
    config = {
        "share": {
            "agents": [],
            "ssh": {"enabled": False, "host": False, "readonly": False},
            "readonly": [".aim"],
            "readwrite": [],
            "hidden": [],
        },
    }

    class Result:
        returncode = 0
        stdout = "C /usr/bin/htop\nA /etc/apt/sources.list.d/foo.list\nA /home/user/note\nA " + str(project / "file") + "\n"

    monkeypatch.setattr(cli, "container_exists", lambda name: True)
    monkeypatch.setattr(cli, "docker", lambda args, **kwargs: Result())

    changes = cli.container_system_changes(project, config, "user", "container")

    assert changes == [("C", "/usr/bin/htop"), ("A", "/etc/apt/sources.list.d/foo.list")]


def test_system_changes_ignores_docker_managed_etc_files(
    workspace: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _home, project = workspace

    class Result:
        returncode = 0
        stdout = "C /etc\nC /etc/hosts\nC /etc/resolv.conf\n"

    monkeypatch.setattr(cli, "container_exists", lambda name: True)
    monkeypatch.setattr(cli, "docker", lambda args, **kwargs: Result())

    assert cli.container_system_changes(project, {}, "user", "container") == []


def test_network_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.platform, "system", lambda: "Linux")

    assert cli.network_args({"network": {"mode": "auto"}}) == ["--network", "host"]
    assert cli.network_args({"network": {"mode": "host"}}) == ["--network", "host"]
    assert cli.network_args({"network": {"mode": "off"}}) == ["--network", "none"]
    assert cli.network_args({"network": {"mode": "bridge", "ports": [3000]}}) == ["-p", "127.0.0.1:3000:3000"]


def test_doctor_detects_outside_project_symlinks(workspace: tuple[Path, Path]) -> None:
    _home, project = workspace
    outside = project.parent / "outside"
    inside = project / "inside"
    outside.mkdir()
    inside.mkdir()
    (project / "outside-link").symlink_to(outside)
    (project / "inside-link").symlink_to(inside)

    found = cli.outside_symlinks(project)

    assert found == [(project / "outside-link", outside)]


def test_ensure_container_builds_expected_docker_run_args(
    workspace: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    home, project = workspace
    calls: list[list[str]] = []

    def fake_docker(args: list[str], *, capture: bool = False, check: bool = True):
        calls.append(args)
        return None

    monkeypatch.setattr(cli, "docker", fake_docker)
    monkeypatch.setattr(cli, "build_image", lambda root: cli.ensure_project(root))
    monkeypatch.setattr(cli, "container_exists", lambda name: False)

    cli.ensure_project(project)
    config = cli.load_toml(project / ".aim" / "config.toml")
    config["network"]["mode"] = "bridge"
    config["network"]["ports"] = [3000]
    config["share"]["agents"] = [{"name": "pi", "host": False}]
    cli.save_project(project, config)

    cli.ensure_container(project)

    run_args = next(args for args in calls if args[:2] == ["run", "-d"])
    specs = mount_specs(run_args)

    assert "--name" in run_args
    assert "--add-host" in run_args
    assert "aim:127.0.0.1" in run_args
    assert f"aim.project={project}" in run_args
    assert "-p" in run_args
    assert "127.0.0.1:3000:3000" in run_args
    assert f"type=bind,source={project},target={project}" in specs
    assert f"type=bind,source={project / '.aim'},target={project / '.aim'},readonly" in specs
    assert f"type=bind,source={home / '.aim' / 'share' / 'agents' / 'pi'},target=/home/{cli.user_spec(config)[0]}/.pi" in specs
    assert run_args[-2:] == ["sleep", "infinity"]
