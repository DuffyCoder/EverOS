"""Tests for openclaw_docker_adapter: OV recall-trace env injection and
plugin stdout capture.

Covers:
  - OV_CURRENT_QUESTION_ID / OV_CURRENT_CONV_ID / OV_RECALL_TRACE_LEVEL
    are injected as -e flags when docker exec runs agent_run
  - OV_RECALL_TRACE_LEVEL passes through host env or defaults to "1"
  - plugin-stdout.log is created at the expected artifacts path
  - _stop_plugin_log_capture terminates the subprocess and closes the file

Run:
    .venv/bin/python -m pytest tests/evaluation/test_openclaw_docker_env_injection.py -v
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from evaluation.src.adapters.openclaw_docker_adapter import (
    PLUGIN_STDOUT_LOG,
    DockerizedOpenclawAdapter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(
    openclaw_cfg: dict | None = None,
    output_dir: Any = None,
) -> DockerizedOpenclawAdapter:
    """Build a minimal DockerizedOpenclawAdapter without running __init__.

    Only sets the attributes read by the methods under test so we don't
    need a real docker image or a real config file.
    """
    adapter = DockerizedOpenclawAdapter.__new__(DockerizedOpenclawAdapter)
    adapter._openclaw_cfg = openclaw_cfg or {
        "memory_mode": "noop",
        "context_engine_mode": "openviking",
        "agent_llm": {
            "env_vars": ["LLM_API_KEY", "OPENVIKING_API_KEY"],
            "model": {"id": "test-model"},
        },
    }
    adapter._docker_cfg = {"add_host_gateway": True}
    adapter.output_dir = output_dir
    adapter._docker_handles = {}
    adapter._plugin_log_procs = {}
    return adapter


def _docker_exec_cmd_from_call(mock_create_subprocess, call_index: int = 0) -> list[str]:
    """Extract the cmd list passed to asyncio.create_subprocess_exec."""
    call = mock_create_subprocess.call_args_list[call_index]
    return list(call.args)


# ---------------------------------------------------------------------------
# Env injection tests
# ---------------------------------------------------------------------------

class TestOvEnvInjection:
    """OV_CURRENT_QUESTION_ID / OV_CURRENT_CONV_ID / OV_RECALL_TRACE_LEVEL
    are injected via docker exec -e when command=agent_run."""

    @pytest.mark.asyncio
    async def test_question_id_injected_into_docker_exec(self):
        """docker exec cmd must include -e OV_CURRENT_QUESTION_ID=<qid>."""
        adapter = _make_adapter()
        container_id = "abc123deadbeef"
        conv_id = "locomo_7"
        qid = "locomo_7_qa9"
        adapter._docker_handles[conv_id] = {"container_id": container_id}

        fake_response = b'{"ok": true, "reply": "test", "stop_reason": "done"}'

        mock_proc = mock.AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = mock.AsyncMock(return_value=(fake_response, b""))

        with mock.patch(
            "evaluation.src.adapters.openclaw_docker_adapter.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ) as mock_exec:
            await adapter._arun_bridge_via_docker(
                conv_id,
                {"command": "agent_run", "session_id": f"{conv_id}__{qid}"},
                question_id=qid,
            )

        cmd = _docker_exec_cmd_from_call(mock_exec)
        assert "-e" in cmd
        assert f"OV_CURRENT_QUESTION_ID={qid}" in cmd

    @pytest.mark.asyncio
    async def test_conv_id_injected_into_docker_exec(self):
        """docker exec cmd must include -e OV_CURRENT_CONV_ID=<conv_id>."""
        adapter = _make_adapter()
        conv_id = "locomo_7"
        qid = "locomo_7_qa3"
        adapter._docker_handles[conv_id] = {"container_id": "abc123"}

        mock_proc = mock.AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = mock.AsyncMock(
            return_value=(b'{"ok": true, "reply": "", "stop_reason": "done"}', b"")
        )

        with mock.patch(
            "evaluation.src.adapters.openclaw_docker_adapter.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ) as mock_exec:
            await adapter._arun_bridge_via_docker(
                conv_id,
                {"command": "agent_run", "session_id": f"{conv_id}__{qid}"},
                question_id=qid,
            )

        cmd = _docker_exec_cmd_from_call(mock_exec)
        assert f"OV_CURRENT_CONV_ID={conv_id}" in cmd

    @pytest.mark.asyncio
    async def test_recall_trace_level_defaults_to_1(self):
        """When OV_RECALL_TRACE_LEVEL is unset, default '1' is injected."""
        adapter = _make_adapter()
        conv_id = "locomo_3"
        qid = "locomo_3_qa1"
        adapter._docker_handles[conv_id] = {"container_id": "cid_001"}

        mock_proc = mock.AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = mock.AsyncMock(
            return_value=(b'{"ok": true, "reply": "", "stop_reason": "done"}', b"")
        )

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OV_RECALL_TRACE_LEVEL", None)
            with mock.patch(
                "evaluation.src.adapters.openclaw_docker_adapter.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ) as mock_exec:
                await adapter._arun_bridge_via_docker(
                    conv_id,
                    {"command": "agent_run", "session_id": f"{conv_id}__{qid}"},
                    question_id=qid,
                )

        cmd = _docker_exec_cmd_from_call(mock_exec)
        assert "OV_RECALL_TRACE_LEVEL=1" in cmd

    @pytest.mark.asyncio
    async def test_recall_trace_level_forwarded_from_env(self, monkeypatch):
        """When OV_RECALL_TRACE_LEVEL is set on the host, it is forwarded."""
        monkeypatch.setenv("OV_RECALL_TRACE_LEVEL", "3")

        adapter = _make_adapter()
        conv_id = "locomo_5"
        qid = "locomo_5_qa2"
        adapter._docker_handles[conv_id] = {"container_id": "cid_002"}

        mock_proc = mock.AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = mock.AsyncMock(
            return_value=(b'{"ok": true, "reply": "", "stop_reason": "done"}', b"")
        )

        with mock.patch(
            "evaluation.src.adapters.openclaw_docker_adapter.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ) as mock_exec:
            await adapter._arun_bridge_via_docker(
                conv_id,
                {"command": "agent_run", "session_id": f"{conv_id}__{qid}"},
                question_id=qid,
            )

        cmd = _docker_exec_cmd_from_call(mock_exec)
        assert "OV_RECALL_TRACE_LEVEL=3" in cmd

    @pytest.mark.asyncio
    async def test_env_not_injected_for_non_agent_run_commands(self):
        """OV_ env vars must NOT be injected for archive_session or index commands."""
        adapter = _make_adapter()
        conv_id = "locomo_7"
        adapter._docker_handles[conv_id] = {"container_id": "cid_003"}

        mock_proc = mock.AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = mock.AsyncMock(
            return_value=(b'{"ok": true}', b"")
        )

        with mock.patch(
            "evaluation.src.adapters.openclaw_docker_adapter.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ) as mock_exec:
            await adapter._arun_bridge_via_docker(
                conv_id,
                {"command": "archive_session", "session_id": f"{conv_id}__qa1"},
                question_id="locomo_7_qa1",
            )

        cmd = _docker_exec_cmd_from_call(mock_exec)
        assert "OV_CURRENT_QUESTION_ID" not in " ".join(cmd)

    @pytest.mark.asyncio
    async def test_env_not_injected_when_question_id_is_none(self):
        """When question_id=None (non-QA bridge calls), no OV_ vars injected."""
        adapter = _make_adapter()
        conv_id = "locomo_7"
        adapter._docker_handles[conv_id] = {"container_id": "cid_004"}

        mock_proc = mock.AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = mock.AsyncMock(
            return_value=(b'{"ok": true}', b"")
        )

        with mock.patch(
            "evaluation.src.adapters.openclaw_docker_adapter.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ) as mock_exec:
            await adapter._arun_bridge_via_docker(
                conv_id,
                {"command": "agent_run", "session_id": f"{conv_id}__bootstrap"},
                question_id=None,
            )

        cmd = _docker_exec_cmd_from_call(mock_exec)
        assert "OV_CURRENT_QUESTION_ID" not in " ".join(cmd)


# ---------------------------------------------------------------------------
# Plugin stdout log capture tests
# ---------------------------------------------------------------------------

class TestPluginLogCapture:
    """_start_plugin_log_capture spawns docker logs -f and writes to the
    correct artifacts path; _stop_plugin_log_capture terminates it."""

    def test_plugin_log_path_layout(self, tmp_path):
        """plugin-stdout.log must land at <output_dir>/artifacts/openclaw/<conv>/."""
        adapter = _make_adapter(output_dir=str(tmp_path))
        conv_id = "locomo_7"
        expected = tmp_path / "artifacts" / "openclaw" / conv_id / PLUGIN_STDOUT_LOG
        assert adapter._plugin_log_path(conv_id) == expected

    def test_plugin_log_path_returns_none_without_output_dir(self):
        """When output_dir is not set, _plugin_log_path returns None gracefully."""
        adapter = _make_adapter(output_dir=None)
        assert adapter._plugin_log_path("locomo_7") is None

    def test_start_plugin_log_capture_spawns_docker_logs(self, tmp_path):
        """_start_plugin_log_capture spawns 'docker logs -f <cid>'."""
        adapter = _make_adapter(output_dir=str(tmp_path))
        conv_id = "locomo_7"
        cid = "testcid123"

        mock_proc = mock.MagicMock(spec=subprocess.Popen)
        with mock.patch(
            "evaluation.src.adapters.openclaw_docker_adapter.subprocess.Popen",
            return_value=mock_proc,
        ) as mock_popen:
            adapter._start_plugin_log_capture(cid, conv_id)

        call_args = mock_popen.call_args
        cmd_called = call_args.args[0]
        assert cmd_called == ["docker", "logs", "-f", cid]

    def test_start_plugin_log_capture_creates_log_file(self, tmp_path):
        """_start_plugin_log_capture creates the parent directory and log file."""
        adapter = _make_adapter(output_dir=str(tmp_path))
        conv_id = "locomo_7"
        cid = "testcid456"

        mock_proc = mock.MagicMock(spec=subprocess.Popen)
        with mock.patch(
            "evaluation.src.adapters.openclaw_docker_adapter.subprocess.Popen",
            return_value=mock_proc,
        ):
            adapter._start_plugin_log_capture(cid, conv_id)

        log_dir = tmp_path / "artifacts" / "openclaw" / conv_id
        assert log_dir.exists()
        # The proc is tracked in _plugin_log_procs.
        assert conv_id in adapter._plugin_log_procs

    def test_start_plugin_log_capture_no_crash_without_output_dir(self):
        """When output_dir is None, _start_plugin_log_capture logs + returns."""
        adapter = _make_adapter(output_dir=None)
        # Should not raise
        adapter._start_plugin_log_capture("somecid", "locomo_7")
        assert "locomo_7" not in adapter._plugin_log_procs

    def test_stop_plugin_log_capture_terminates_proc(self, tmp_path):
        """_stop_plugin_log_capture calls proc.terminate() when proc is alive."""
        adapter = _make_adapter(output_dir=str(tmp_path))
        conv_id = "locomo_7"

        mock_proc = mock.MagicMock(spec=subprocess.Popen)
        mock_proc.poll.return_value = None  # proc is still running
        mock_proc.wait.return_value = 0
        mock_file = mock.MagicMock()
        adapter._plugin_log_procs[conv_id] = (mock_proc, mock_file)

        adapter._stop_plugin_log_capture(conv_id)

        mock_proc.terminate.assert_called_once()
        mock_file.close.assert_called_once()
        assert conv_id not in adapter._plugin_log_procs

    def test_stop_plugin_log_capture_noop_when_not_running(self, tmp_path):
        """_stop_plugin_log_capture is a no-op when no proc is registered."""
        adapter = _make_adapter(output_dir=str(tmp_path))
        # Should not raise
        adapter._stop_plugin_log_capture("nonexistent_conv")

    def test_stop_plugin_log_capture_skips_terminate_when_proc_exited(self, tmp_path):
        """If the proc has already exited (poll != None), terminate is not called."""
        adapter = _make_adapter(output_dir=str(tmp_path))
        conv_id = "locomo_9"

        mock_proc = mock.MagicMock(spec=subprocess.Popen)
        mock_proc.poll.return_value = 0  # already exited
        mock_file = mock.MagicMock()
        adapter._plugin_log_procs[conv_id] = (mock_proc, mock_file)

        adapter._stop_plugin_log_capture(conv_id)

        mock_proc.terminate.assert_not_called()
        mock_file.close.assert_called_once()
