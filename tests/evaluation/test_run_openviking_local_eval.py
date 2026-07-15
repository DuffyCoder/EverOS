import os
import shutil
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "openclaw-eval" / "scripts" / "run_openviking_local_eval.sh"
WRAPPER = REPO_ROOT / "build.sh"
EXTERNAL_COMMANDS = ("ss", "rm", "npm", "curl", "setsid", "sleep", "git")


def _environment_with_command_traps(tmp_path: Path) -> tuple[dict[str, str], Path]:
    call_log = tmp_path / "external-commands.log"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for command in EXTERNAL_COMMANDS:
        executable = fake_bin / command
        executable.write_text(
            f'#!/usr/bin/env bash\necho "{command} $*" >> "{call_log}"\nexit 99\n',
            encoding="utf-8",
        )
        executable.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    return env, call_log


def _valid_mock_runner_environment(
    tmp_path: Path,
) -> tuple[dict[str, str], dict[str, Path]]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    call_log = tmp_path / "commands.log"
    eval_log = tmp_path / "evaluation.log"
    bash_env = tmp_path / "bash-env"
    bash_env.write_text("enable -n kill\n", encoding="utf-8")

    command_bodies = {
        "ss": "printf '%s' \"${FAKE_SS_OUTPUT:-}\"\nexit \"${FAKE_SS_STATUS:-0}\"\n",
        "kill": "exit \"${FAKE_KILL_STATUS:-0}\"\n",
        "curl": "printf '%s\\n' \"${FAKE_CURL_OUTPUT:-{\\\"status\\\":\\\"ok\\\"}}\"\nexit \"${FAKE_CURL_STATUS:-0}\"\n",
        "setsid": "exit \"${FAKE_SETSID_STATUS:-0}\"\n",
        "git": "[[ \"$*\" == *rev-parse* ]] && echo abc123 || echo main\nexit 0\n",
    }
    for command in (*EXTERNAL_COMMANDS, "kill"):
        executable = fake_bin / command
        body = command_bodies.get(command, "exit 0\n")
        executable.write_text(
            "#!/usr/bin/env bash\n"
            f"printf '{command}' >> \"$FAKE_COMMAND_LOG\"\n"
            "printf ' <%s>' \"$@\" >> \"$FAKE_COMMAND_LOG\"\n"
            "printf '\\n' >> \"$FAKE_COMMAND_LOG\"\n"
            + body,
            encoding="utf-8",
        )
        executable.chmod(0o755)

    fork = tmp_path / "fork"
    (fork / "openviking").mkdir(parents=True)
    (fork / "pyproject.toml").touch()
    reset_target = fork / ".ovdata"
    reset_target.mkdir()
    sentinel = reset_target / "sentinel"
    sentinel.write_text("must survive", encoding="utf-8")
    plugin = fork / "examples" / "openclaw-plugin"
    (plugin / "node_modules").mkdir(parents=True)
    config = tmp_path / "config.json"
    config.touch()
    server = fork / ".venv" / "bin" / "openviking-server"
    server.parent.mkdir(parents=True)
    server.write_text("#!/usr/bin/env bash\nexit 88\n", encoding="utf-8")
    server.chmod(0o755)
    eval_python = tmp_path / "python"
    eval_python.write_text(
        "#!/usr/bin/env bash\ntouch \"$FAKE_EVAL_LOG\"\n",
        encoding="utf-8",
    )
    eval_python.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "BASH_ENV": str(bash_env),
            "FAKE_COMMAND_LOG": str(call_log),
            "FAKE_EVAL_LOG": str(eval_log),
            "OPENVIKING_FORK": str(fork),
            "OPENVIKING_PLUGIN_DIR": str(plugin),
            "OPENVIKING_CONFIG": str(config),
            "OPENVIKING_SERVER_BIN": str(server),
            "EVAL_PYTHON": str(eval_python),
            "EVAL_LOG_DIR": str(tmp_path / "logs"),
            "TCMALLOC_PATH": "",
        }
    )
    paths = {
        "call_log": call_log,
        "eval_log": eval_log,
        "sentinel": sentinel,
        "fork": fork,
        "server": server,
    }
    return env, paths


def test_portable_runner_is_installed() -> None:
    assert RUNNER.is_file()


def test_root_build_script_is_a_thin_wrapper() -> None:
    source = WRAPPER.read_text(encoding="utf-8")

    assert 'SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")"' in source
    assert 'exec "$SCRIPT_DIR/openclaw-eval/scripts/run_openviking_local_eval.sh" "$@"' in source
    for destructive_snippet in ("rm -rf", "kill ", "npm install", "curl "):
        assert destructive_snippet not in source


def test_build_help_delegates_from_an_unrelated_working_directory(tmp_path: Path) -> None:
    result = subprocess.run(
        [str(WRAPPER), "--help"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Usage: build.sh" in result.stdout
    assert "OPENVIKING_FORK" in result.stdout


def test_dry_run_prints_all_overrides_without_invoking_external_commands(
    tmp_path: Path,
) -> None:
    env, call_log = _environment_with_command_traps(tmp_path)
    overrides = {
        "OPENVIKING_FORK": str(tmp_path / "fork override"),
        "OPENVIKING_PLUGIN_DIR": str(tmp_path / "plugin override"),
        "OPENVIKING_CONFIG": str(tmp_path / "config override.json"),
        "OPENVIKING_SERVER_BIN": str(tmp_path / "server override"),
        "EVAL_PYTHON": str(tmp_path / "python override"),
        "EVAL_SYSTEM": "system-override",
        "EVAL_LOG_DIR": str(tmp_path / "log override"),
        "TCMALLOC_PATH": str(tmp_path / "allocator override.so"),
    }
    env.update(overrides)

    result = subprocess.run(
        [str(WRAPPER), "--dry-run", "portable-run", "--from-conv", "3"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert f"repository: {REPO_ROOT}" in result.stdout
    assert "run-name: portable-run" in result.stdout
    assert "evaluation-args: --from-conv 3" in result.stdout
    assert f"log: {overrides['EVAL_LOG_DIR']}/ov-server.log" in result.stdout
    for value in overrides.values():
        assert value in result.stdout
    assert not call_log.exists()


def test_dry_run_canonicalizes_relative_symlink_and_dash_prefixed_paths(
    tmp_path: Path,
) -> None:
    canonical_fork = tmp_path / "canonical OpenViking fork"
    canonical_fork.mkdir()
    (tmp_path / "fork-link").symlink_to(canonical_fork, target_is_directory=True)
    allocator = tmp_path / "allocator target.so"
    allocator.touch()
    (tmp_path / "allocator-link.so").symlink_to(allocator)

    env = os.environ.copy()
    env.update(
        {
            "OPENVIKING_FORK": "fork-link",
            "OPENVIKING_CONFIG": "-config/config file.json",
            "EVAL_PYTHON": "python dir/../-python",
            "EVAL_LOG_DIR": "logs/../runner logs",
            "TCMALLOC_PATH": "allocator-link.so",
        }
    )
    env.pop("OPENVIKING_PLUGIN_DIR", None)
    env.pop("OPENVIKING_SERVER_BIN", None)

    result = subprocess.run(
        [str(WRAPPER), "--dry-run", "canonical-paths"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert f"fork: {canonical_fork}" in result.stdout
    assert f"plugin: {canonical_fork / 'examples/openclaw-plugin'}" in result.stdout
    assert f"server: {canonical_fork / '.venv/bin/openviking-server'}" in result.stdout
    assert f"config: {tmp_path / '-config/config file.json'}" in result.stdout
    assert f"python: {tmp_path / '-python'}" in result.stdout
    assert f"log-dir: {tmp_path / 'runner logs'}" in result.stdout
    assert f"tcmalloc: {allocator}" in result.stdout


def test_noninteractive_run_requires_explicit_reset_before_external_work(
    tmp_path: Path,
) -> None:
    env, call_log = _environment_with_command_traps(tmp_path)
    result = subprocess.run(
        [str(WRAPPER), "guarded-run"],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "non-interactive" in result.stderr
    assert "--yes-reset" in result.stderr
    assert not call_log.exists()


def test_required_paths_are_validated_before_process_or_reset_commands(
    tmp_path: Path,
) -> None:
    env, call_log = _environment_with_command_traps(tmp_path)
    missing_fork = tmp_path / "missing-fork"
    env.update(
        {
            "OPENVIKING_FORK": str(missing_fork),
            "OPENVIKING_PLUGIN_DIR": str(tmp_path / "missing-plugin"),
            "OPENVIKING_CONFIG": str(tmp_path / "missing-config"),
            "OPENVIKING_SERVER_BIN": str(tmp_path / "missing-server"),
            "EVAL_PYTHON": str(tmp_path / "missing-python"),
            "EVAL_LOG_DIR": str(tmp_path / "logs"),
        }
    )

    result = subprocess.run(
        [str(WRAPPER), "--yes-reset", "invalid-path-run"],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert f"required directory does not exist: {missing_fork}" in result.stderr
    assert not call_log.exists()


def test_root_and_unmarked_forks_are_rejected_before_external_work(
    tmp_path: Path,
) -> None:
    for label, fork, expected_error in (
        ("root", Path("/"), "refusing unsafe OpenViking fork root"),
        ("unmarked", tmp_path / "not-an-openviking-checkout", "missing checkout markers"),
    ):
        scenario = tmp_path / label
        scenario.mkdir()
        env, call_log = _environment_with_command_traps(scenario)
        if fork != Path("/"):
            fork.mkdir()
        plugin = scenario / "plugin"
        (plugin / "node_modules").mkdir(parents=True)
        config = scenario / "config.json"
        config.touch()
        server = scenario / "server"
        server.touch(mode=0o755)
        eval_python = scenario / "python"
        eval_python.touch(mode=0o755)
        log_dir = scenario / "logs"
        env.update(
            {
                "OPENVIKING_FORK": str(fork),
                "OPENVIKING_PLUGIN_DIR": str(plugin),
                "OPENVIKING_CONFIG": str(config),
                "OPENVIKING_SERVER_BIN": str(server),
                "EVAL_PYTHON": str(eval_python),
                "EVAL_LOG_DIR": str(log_dir),
            }
        )

        result = subprocess.run(
            [str(WRAPPER), "--yes-reset", f"unsafe-{label}"],
            cwd=scenario,
            env=env,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode != 0
        assert expected_error in result.stderr
        assert not call_log.exists()
        assert not log_dir.exists()


def test_external_paths_inside_reset_target_are_rejected_before_writes(
    tmp_path: Path,
) -> None:
    for variable in (
        "OPENVIKING_PLUGIN_DIR",
        "OPENVIKING_CONFIG",
        "OPENVIKING_SERVER_BIN",
        "EVAL_PYTHON",
        "EVAL_LOG_DIR",
        "TCMALLOC_PATH",
    ):
        scenario = tmp_path / variable.lower()
        scenario.mkdir()
        env, call_log = _environment_with_command_traps(scenario)
        fork = scenario / "fork"
        (fork / "openviking").mkdir(parents=True)
        (fork / "pyproject.toml").touch()
        reset_target = fork / ".ovdata"
        reset_target.mkdir()
        sentinel = reset_target / "sentinel"
        sentinel.write_text("must survive", encoding="utf-8")

        plugin = fork / "examples" / "openclaw-plugin"
        (plugin / "node_modules").mkdir(parents=True)
        config = scenario / "config.json"
        config.touch()
        server = fork / ".venv" / "bin" / "openviking-server"
        server.parent.mkdir(parents=True)
        server.touch(mode=0o755)
        eval_python = scenario / "python"
        eval_python.touch(mode=0o755)
        log_dir = scenario / "logs"

        dangerous = reset_target / variable.lower()
        if variable in {"OPENVIKING_PLUGIN_DIR", "EVAL_LOG_DIR"}:
            if variable == "OPENVIKING_PLUGIN_DIR":
                (dangerous / "node_modules").mkdir(parents=True)
        else:
            dangerous.touch(mode=0o755)

        env.update(
            {
                "OPENVIKING_FORK": str(fork),
                "OPENVIKING_PLUGIN_DIR": str(plugin),
                "OPENVIKING_CONFIG": str(config),
                "OPENVIKING_SERVER_BIN": str(server),
                "EVAL_PYTHON": str(eval_python),
                "EVAL_LOG_DIR": str(log_dir),
                "TCMALLOC_PATH": "",
                variable: str(dangerous),
            }
        )

        result = subprocess.run(
            [str(WRAPPER), "--yes-reset", f"inside-reset-{variable.lower()}"],
            cwd=scenario,
            env=env,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode != 0
        assert f"{variable} is inside reset target" in result.stderr
        assert not call_log.exists()
        assert sentinel.read_text(encoding="utf-8") == "must survive"
        if variable == "EVAL_LOG_DIR":
            assert not dangerous.exists()


def test_ambiguous_or_unsafe_listener_output_fails_closed_without_side_effects(
    tmp_path: Path,
) -> None:
    cases = (
        (
            "wrong-port",
            'LISTEN 0 128 0.0.0.0:19330 0.0.0.0:* users:(("ov",pid=999999999,fd=3))',
            "unexpected listener endpoint",
        ),
        (
            "pid-zero",
            'LISTEN 0 128 0.0.0.0:1933 0.0.0.0:* users:(("ov",pid=0,fd=3))',
            "unsafe listener PID",
        ),
        (
            "pid-one",
            'LISTEN 0 128 0.0.0.0:1933 0.0.0.0:* users:(("ov",pid=1,fd=3))',
            "unsafe listener PID",
        ),
        (
            "multiple-pids",
            '\n'.join(
                (
                    'LISTEN 0 128 127.0.0.1:1933 0.0.0.0:* users:(("ov",pid=999999997,fd=3))',
                    'LISTEN 0 128 [::1]:1933 [::]:* users:(("ov",pid=999999998,fd=4))',
                )
            ),
            "multiple listener PIDs",
        ),
    )

    for label, ss_output, expected_error in cases:
        scenario = tmp_path / label
        scenario.mkdir()
        fake_bin = scenario / "fake-bin"
        fake_bin.mkdir()
        call_log = scenario / "commands.log"
        eval_log = scenario / "evaluation.log"
        bash_env = scenario / "bash-env"
        bash_env.write_text("enable -n kill\n", encoding="utf-8")

        for command in (*EXTERNAL_COMMANDS, "kill"):
            executable = fake_bin / command
            if command == "ss":
                body = "printf '%s\\n' \"$FAKE_SS_OUTPUT\"\n"
            elif command == "git":
                body = "[[ \"$*\" == *rev-parse* ]] && echo abc123 || echo main\n"
            elif command == "curl":
                body = "echo '{\"status\":\"ok\"}'\n"
            else:
                body = ""
            executable.write_text(
                "#!/usr/bin/env bash\n"
                f"printf '{command}' >> \"$FAKE_COMMAND_LOG\"\n"
                "printf ' <%s>' \"$@\" >> \"$FAKE_COMMAND_LOG\"\n"
                "printf '\\n' >> \"$FAKE_COMMAND_LOG\"\n"
                f"{body}"
                "exit 0\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)

        fork = scenario / "fork"
        (fork / "openviking").mkdir(parents=True)
        (fork / "pyproject.toml").touch()
        reset_target = fork / ".ovdata"
        reset_target.mkdir()
        sentinel = reset_target / "sentinel"
        sentinel.write_text("must survive", encoding="utf-8")
        plugin = fork / "examples" / "openclaw-plugin"
        (plugin / "node_modules").mkdir(parents=True)
        config = scenario / "config.json"
        config.touch()
        server = fork / ".venv" / "bin" / "openviking-server"
        server.parent.mkdir(parents=True)
        server.touch(mode=0o755)
        eval_python = scenario / "python"
        eval_python.write_text(
            "#!/usr/bin/env bash\ntouch \"$FAKE_EVAL_LOG\"\n",
            encoding="utf-8",
        )
        eval_python.chmod(0o755)

        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{fake_bin}:{env['PATH']}",
                "BASH_ENV": str(bash_env),
                "FAKE_COMMAND_LOG": str(call_log),
                "FAKE_EVAL_LOG": str(eval_log),
                "FAKE_SS_OUTPUT": ss_output,
                "OPENVIKING_FORK": str(fork),
                "OPENVIKING_PLUGIN_DIR": str(plugin),
                "OPENVIKING_CONFIG": str(config),
                "OPENVIKING_SERVER_BIN": str(server),
                "EVAL_PYTHON": str(eval_python),
                "EVAL_LOG_DIR": str(scenario / "logs"),
                "TCMALLOC_PATH": "",
            }
        )

        result = subprocess.run(
            [str(WRAPPER), "--yes-reset", f"listener-{label}"],
            cwd=scenario,
            env=env,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode != 0
        assert expected_error in result.stderr
        commands = call_log.read_text(encoding="utf-8")
        assert "ss <-H> <-ltnp> <sport = :1933>" in commands
        for forbidden in ("kill", "rm", "setsid", "curl"):
            assert f"\n{forbidden}" not in f"\n{commands}"
        assert not eval_log.exists()
        assert not (scenario / "logs").exists()
        assert sentinel.read_text(encoding="utf-8") == "must survive"


def test_listener_is_resampled_after_npm_before_any_kill(tmp_path: Path) -> None:
    stale_pid = "999999997"
    for label, refreshed_pid in (("pid-changed", "999999998"), ("port-empty", "")):
        scenario = tmp_path / label
        scenario.mkdir()
        env, paths = _valid_mock_runner_environment(scenario)
        plugin = Path(env["OPENVIKING_PLUGIN_DIR"])
        (plugin / "node_modules").rmdir()
        npm_finished = scenario / "npm-finished"

        ss = scenario / "fake-bin" / "ss"
        ss.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'ss' >> \"$FAKE_COMMAND_LOG\"\n"
            "printf ' <%s>' \"$@\" >> \"$FAKE_COMMAND_LOG\"\n"
            "printf '\\n' >> \"$FAKE_COMMAND_LOG\"\n"
            "pid=\"$STALE_LISTENER_PID\"\n"
            "if [[ -e \"$FAKE_NPM_FINISHED\" ]]; then\n"
            "  pid=\"$REFRESHED_LISTENER_PID\"\n"
            "fi\n"
            "if [[ -n \"$pid\" ]]; then\n"
            "  printf 'LISTEN 0 128 127.0.0.1:1933 0.0.0.0:* "
            "users:((\\\"ov\\\",pid=%s,fd=3))\\n' \"$pid\"\n"
            "fi\n",
            encoding="utf-8",
        )
        ss.chmod(0o755)

        npm = scenario / "fake-bin" / "npm"
        npm.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'npm' >> \"$FAKE_COMMAND_LOG\"\n"
            "printf ' <%s>' \"$@\" >> \"$FAKE_COMMAND_LOG\"\n"
            "printf '\\n' >> \"$FAKE_COMMAND_LOG\"\n"
            "touch \"$FAKE_NPM_FINISHED\"\n",
            encoding="utf-8",
        )
        npm.chmod(0o755)

        env.update(
            {
                "STALE_LISTENER_PID": stale_pid,
                "REFRESHED_LISTENER_PID": refreshed_pid,
                "FAKE_NPM_FINISHED": str(npm_finished),
                "FAKE_KILL_STATUS": "1",
                "FAKE_SETSID_STATUS": "17",
            }
        )

        result = subprocess.run(
            [str(WRAPPER), "--yes-reset", f"npm-resample-{label}"],
            cwd=scenario,
            env=env,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode != 0
        commands = paths["call_log"].read_text(encoding="utf-8")
        assert "npm <install>" in commands
        assert f"kill <{stale_pid}>" not in commands
        if refreshed_pid:
            assert f"kill <{refreshed_pid}>" in commands
            assert f"failed to stop OpenViking listener PID {refreshed_pid}" in result.stderr
        else:
            terminating_kills = [
                line
                for line in commands.splitlines()
                if line.startswith("kill") and "<-0>" not in line
            ]
            assert terminating_kills == []
            assert "server launch process exited before health was confirmed" in result.stderr


def test_kill_failure_stops_before_reset_or_evaluation(tmp_path: Path) -> None:
    env, paths = _valid_mock_runner_environment(tmp_path)
    env.update(
        {
            "FAKE_SS_OUTPUT": (
                'LISTEN 0 128 127.0.0.1:1933 0.0.0.0:* '
                'users:(("ov",pid=999999999,fd=3))'
            ),
            "FAKE_KILL_STATUS": "42",
        }
    )

    result = subprocess.run(
        [str(WRAPPER), "--yes-reset", "kill-failure"],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "failed to stop OpenViking listener PID 999999999" in result.stderr
    commands = paths["call_log"].read_text(encoding="utf-8")
    assert "kill <999999999>" in commands
    assert "\nrm" not in f"\n{commands}"
    assert not paths["eval_log"].exists()
    assert not (tmp_path / "logs").exists()
    assert paths["sentinel"].read_text(encoding="utf-8") == "must survive"


def test_listener_must_release_port_before_reset(tmp_path: Path) -> None:
    env, paths = _valid_mock_runner_environment(tmp_path)
    env["FAKE_SS_OUTPUT"] = (
        'LISTEN 0 128 127.0.0.1:1933 0.0.0.0:* '
        'users:(("ov",pid=999999999,fd=3))'
    )

    result = subprocess.run(
        [str(WRAPPER), "--yes-reset", "stale-listener"],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "listener did not release port 1933" in result.stderr
    commands = paths["call_log"].read_text(encoding="utf-8")
    assert commands.count("ss <-H> <-ltnp> <sport = :1933>") > 1
    assert "\nrm" not in f"\n{commands}"
    assert not paths["eval_log"].exists()
    assert not (tmp_path / "logs").exists()
    assert paths["sentinel"].read_text(encoding="utf-8") == "must survive"


def test_missing_ss_is_rejected_before_any_write(tmp_path: Path) -> None:
    env, paths = _valid_mock_runner_environment(tmp_path)
    restricted_bin = tmp_path / "required-bin"
    restricted_bin.mkdir()
    for command in ("bash", "dirname", "realpath"):
        resolved = shutil.which(command)
        assert resolved is not None
        (restricted_bin / command).symlink_to(resolved)
    env["PATH"] = str(restricted_bin)

    result = subprocess.run(
        [str(WRAPPER), "--yes-reset", "missing-ss"],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "required command not found: ss" in result.stderr
    assert not paths["call_log"].exists()
    assert not (tmp_path / "logs").exists()
    assert paths["sentinel"].read_text(encoding="utf-8") == "must survive"


def test_ss_failure_stops_before_reset_or_evaluation(tmp_path: Path) -> None:
    env, paths = _valid_mock_runner_environment(tmp_path)
    env["FAKE_SS_STATUS"] = "23"

    result = subprocess.run(
        [str(WRAPPER), "--yes-reset", "ss-failure"],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "ss failed while inspecting the listener" in result.stderr
    commands = paths["call_log"].read_text(encoding="utf-8")
    assert "ss <-H> <-ltnp> <sport = :1933>" in commands
    assert "\nrm" not in f"\n{commands}"
    assert not paths["eval_log"].exists()
    assert not (tmp_path / "logs").exists()
    assert paths["sentinel"].read_text(encoding="utf-8") == "must survive"


def test_exited_server_process_cannot_be_mistaken_for_healthy_endpoint(
    tmp_path: Path,
) -> None:
    env, paths = _valid_mock_runner_environment(tmp_path)
    env.update(
        {
            "FAKE_SS_OUTPUT": "",
            "FAKE_KILL_STATUS": "1",
            "FAKE_CURL_STATUS": "0",
            "FAKE_SETSID_STATUS": "17",
        }
    )

    result = subprocess.run(
        [str(WRAPPER), "--yes-reset", "exited-server"],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "server launch process exited before health was confirmed" in result.stderr
    commands = paths["call_log"].read_text(encoding="utf-8")
    assert "setsid" in commands
    assert "kill <-0>" in commands
    assert not paths["eval_log"].exists()


def test_old_healthy_endpoint_without_new_listener_cannot_start_evaluation(
    tmp_path: Path,
) -> None:
    env, paths = _valid_mock_runner_environment(tmp_path)
    env.update(
        {
            "FAKE_SS_OUTPUT": "",
            "FAKE_KILL_STATUS": "0",
            "FAKE_CURL_STATUS": "0",
        }
    )

    result = subprocess.run(
        [str(WRAPPER), "--yes-reset", "stale-health"],
        cwd=tmp_path,
        env=env,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "new listener was not confirmed" in result.stderr
    commands = paths["call_log"].read_text(encoding="utf-8")
    assert commands.count("ss <-H> <-ltnp> <sport = :1933>") > 1
    assert "curl" in commands
    assert not paths["eval_log"].exists()


def test_authorized_run_preserves_server_setup_mount_and_evaluation_arguments(
    tmp_path: Path,
) -> None:
    for label, use_tcmalloc in (("without-tcmalloc", False), ("with-tcmalloc", True)):
        scenario = tmp_path / label
        scenario.mkdir()
        fake_bin = scenario / "fake-bin"
        fake_bin.mkdir()
        call_log = scenario / "external-commands.log"
        eval_log = scenario / "evaluation.log"
        server_pid_file = scenario / "server.pid"
        server_cwd_file = scenario / "server.cwd"

        for command in EXTERNAL_COMMANDS:
            executable = fake_bin / command
            if command == "ss":
                behavior = (
                    'if [[ -s "$FAKE_SERVER_PID_FILE" ]]; then\n'
                    '  pid="$(<"$FAKE_SERVER_PID_FILE")"\n'
                    "  printf 'LISTEN 0 128 127.0.0.1:1933 0.0.0.0:* "
                    "users:((\\\"ov\\\",pid=%s,fd=3))\\n' \"$pid\"\n"
                    "fi\n"
                )
            elif command == "setsid":
                behavior = (
                    'printf \'%s\' "$$" > "$FAKE_SERVER_PID_FILE"\n'
                    'printf \'%s\' "$PWD" > "$FAKE_SERVER_CWD_FILE"\n'
                    "for _ in {1..500}; do\n"
                    '  [[ -e "$FAKE_EVAL_LOG" ]] && exit 0\n'
                    "  /bin/sleep 0.01\n"
                    "done\n"
                )
            elif command == "curl":
                behavior = "printf '%s\\n' '{\"status\":\"ok\"}'\n"
            elif command == "git":
                behavior = "[[ \"$*\" == *rev-parse* ]] && echo abc123 || echo main\n"
            else:
                behavior = ""
            executable.write_text(
                "#!/usr/bin/env bash\n"
                f"printf '{command}' >> \"$FAKE_COMMAND_LOG\"\n"
                "printf ' <%s>' \"$@\" >> \"$FAKE_COMMAND_LOG\"\n"
                "printf '\\n' >> \"$FAKE_COMMAND_LOG\"\n"
                f"{behavior}"
                "exit 0\n",
                encoding="utf-8",
            )
            executable.chmod(0o755)

        fork = scenario / "OpenViking fork"
        (fork / "openviking").mkdir(parents=True)
        (fork / "pyproject.toml").touch()
        plugin = fork / "examples" / "openclaw-plugin"
        (plugin / "node_modules").mkdir(parents=True)
        reset_target = fork / ".ovdata"
        reset_target.mkdir()
        sentinel = reset_target / "sentinel"
        sentinel.write_text("keep test data", encoding="utf-8")
        config = scenario / "ov config.json"
        config.write_text("{}\n", encoding="utf-8")
        server = scenario / "openviking server"
        server.write_text("#!/usr/bin/env bash\nexit 88\n", encoding="utf-8")
        server.chmod(0o755)
        eval_python = scenario / "evaluation python"
        eval_python.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'cwd=%s\\n' \"$PWD\" > \"$FAKE_EVAL_LOG\"\n"
            "printf 'mount=%s\\n' \"${OPENCLAW_EXTRA_MOUNTS:-}\" >> \"$FAKE_EVAL_LOG\"\n"
            "printf 'args=' >> \"$FAKE_EVAL_LOG\"\n"
            "printf ' <%s>' \"$@\" >> \"$FAKE_EVAL_LOG\"\n"
            "printf '\\n' >> \"$FAKE_EVAL_LOG\"\n",
            encoding="utf-8",
        )
        eval_python.chmod(0o755)
        allocator = scenario / "lib tcmalloc.so"
        if use_tcmalloc:
            allocator.touch()

        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{fake_bin}:{env['PATH']}",
                "FAKE_COMMAND_LOG": str(call_log),
                "FAKE_EVAL_LOG": str(eval_log),
                "FAKE_SERVER_PID_FILE": str(server_pid_file),
                "FAKE_SERVER_CWD_FILE": str(server_cwd_file),
                "OPENVIKING_FORK": str(fork),
                "OPENVIKING_PLUGIN_DIR": str(plugin),
                "OPENVIKING_CONFIG": str(config),
                "OPENVIKING_SERVER_BIN": str(server),
                "EVAL_PYTHON": str(eval_python),
                "EVAL_SYSTEM": "portable-system",
                "EVAL_LOG_DIR": str(scenario / "runner logs"),
                "TCMALLOC_PATH": str(allocator) if use_tcmalloc else "",
                "HTTP_PROXY": "http://must-be-removed.invalid",
                "HTTPS_PROXY": "http://must-be-removed.invalid",
                "ALL_PROXY": "socks5://must-be-removed.invalid",
            }
        )

        result = subprocess.run(
            [
                str(WRAPPER),
                "legacy-positional-run",
                "--yes-reset",
                "--from-conv",
                "0",
                "--to-conv",
                "1",
            ],
            cwd=scenario,
            env=env,
            stdin=subprocess.DEVNULL,
            text=True,
            capture_output=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        evaluation = eval_log.read_text(encoding="utf-8")
        assert f"cwd={REPO_ROOT}" in evaluation
        assert f"mount={plugin}:/app/extensions/openviking:ro" in evaluation
        assert " <-m> <evaluation.cli>" in evaluation
        assert " <--dataset> <locomo>" in evaluation
        assert " <--system> <portable-system>" in evaluation
        assert " <--run-name> <legacy-positional-run>" in evaluation
        assert " <--from-conv> <0> <--to-conv> <1>" in evaluation

        commands = call_log.read_text(encoding="utf-8")
        assert f"rm <-rf> <--> <{reset_target}>" in commands
        setsid_line = next(line for line in commands.splitlines() if line.startswith("setsid"))
        assert "setsid <nohup> <env>" in setsid_line
        for proxy_name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "http_proxy",
            "https_proxy",
            "ALL_PROXY",
            "all_proxy",
        ):
            assert f"<-u> <{proxy_name}>" in setsid_line
        assert f"<{server}> <--host> <0.0.0.0> <--port> <1933> <--config> <{config}>" in setsid_line
        if use_tcmalloc:
            assert f"<LD_PRELOAD={allocator}>" in setsid_line
        else:
            assert "LD_PRELOAD=" not in setsid_line
        assert server_cwd_file.read_text(encoding="utf-8") == str(fork)
        assert "curl" in commands
        assert "npm" not in commands
        assert sentinel.read_text(encoding="utf-8") == "keep test data"
