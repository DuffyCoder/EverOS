"""Tests for in-container streaming usage compat jq patch."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from evaluation.src.adapters.openclaw.config_patches import STREAMING_USAGE_COMPAT_JQ


def test_jq_patch_adds_streaming_usage_compat(tmp_path: Path):
    cfg = {
        "models": {
            "providers": {
                "sophnet": {
                    "models": [
                        {"id": "gpt-4.1-mini", "name": "mini"},
                    ],
                },
            },
        },
    }
    path = tmp_path / "openclaw.docker.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")

    proc = subprocess.run(
        ["jq", STREAMING_USAGE_COMPAT_JQ, str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    patched = json.loads(proc.stdout)
    assert patched["models"]["providers"]["sophnet"]["models"][0]["compat"] == {
        "supportsUsageInStreaming": True,
    }


def test_jq_patch_preserves_existing_compat_fields(tmp_path: Path):
    cfg = {
        "models": {
            "providers": {
                "sophnet": {
                    "models": [
                        {
                            "id": "m",
                            "compat": {"supportsStrictMode": False},
                        },
                    ],
                },
            },
        },
    }
    path = tmp_path / "openclaw.docker.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")

    proc = subprocess.run(
        ["jq", STREAMING_USAGE_COMPAT_JQ, str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    patched = json.loads(proc.stdout)
    compat = patched["models"]["providers"]["sophnet"]["models"][0]["compat"]
    assert compat["supportsUsageInStreaming"] is True
    assert compat["supportsStrictMode"] is False
