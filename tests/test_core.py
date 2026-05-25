from __future__ import annotations

from pathlib import Path

import pytest

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


def test_init_creates_project_files_with_agent_tools(workspace: tuple[Path, Path]) -> None:
    _home, project = workspace

    cli.ensure_global()
    cli.ensure_project(project)

    assert (project / ".aim" / "Dockerfile").exists()
    assert (project / ".aim" / "config.toml").exists()

    config = cli.load_toml(project / ".aim" / "config.toml")
    assert config["ignore"]["paths"] == [".aim", ".env", ".env.local"]
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


def test_unshare_removes_project_share(workspace: tuple[Path, Path]) -> None:
    _home, project = workspace

    cli.share_agent("pi", host=False)
    cli.unshare_agent("pi")

    config = cli.load_toml(project / ".aim" / "config.toml")
    assert config["share"]["agents"] == []


def test_mounts_include_project_masks_and_project_shares(workspace: tuple[Path, Path]) -> None:
    home, project = workspace
    (project / ".aim").mkdir()
    (project / ".env").write_text("SECRET=yes")
    (home / ".pi").mkdir()
    downloads = home / "Downloads"
    downloads.mkdir()
    gitconfig = home / ".gitconfig"
    gitconfig.write_text("[user]\n")

    config = {
        "ignore": {"paths": [".aim", ".env"]},
        "share": {
            "agents": [{"name": "pi", "host": True}],
            "ssh": {"enabled": False, "host": False, "readonly": False},
            "dirs": [{"path": str(downloads), "readonly": True}],
            "files": [{"path": str(gitconfig), "target": "~/.gitconfig", "readonly": True}],
        },
    }

    specs = mount_specs(cli.all_mounts(project, config, "hannes"))

    assert f"type=bind,source={project},target={project}" in specs
    assert f"type=tmpfs,target={project / '.aim'},tmpfs-size=1048576" in specs
    assert f"type=bind,source=/dev/null,target={project / '.env'},readonly" in specs
    assert f"type=bind,source={home / '.pi'},target=/home/hannes/.pi" in specs
    assert f"type=bind,source={downloads},target={downloads},readonly" in specs
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
        "ignore": {"paths": [".aim"]},
        "share": {
            "agents": [],
            "ssh": {"enabled": False, "host": False, "readonly": False},
            "dirs": [],
            "files": [],
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
    assert f"type=bind,source={home / '.aim' / 'share' / 'agents' / 'pi'},target=/home/{cli.user_spec(config)[0]}/.pi" in specs
    assert run_args[-2:] == ["sleep", "infinity"]
