"""Stage 3 Phase 3 — engine_import_history dispatcher unit tests.

The bridge's `engine_import_history` RPC mirrors the production
turn-finalization precedence from openclaw's
`finalizeAttemptContextEngineTurn` (attempt.context-engine-helpers.ts:80):

    if engine.afterTurn:    afterTurn(...)
    elif engine.ingestBatch: ingestBatch(newMessages)
    else:                    for msg in newMessages: ingest(msg)

For an *import* (no prior session state), prePromptMessageCount=0 so the
"new messages" slice equals the entire history.

Tests target the pure dispatcher in openclaw_eval_bridge_lib.mjs by
spawning Node with a stub engine inline. No openclaw runtime needed.

Run:
    .venv/bin/python -m pytest tests/evaluation/test_openclaw_bridge_engine_import.py -v
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).parents[2]
LIB_PATH = REPO_ROOT / "evaluation" / "scripts" / "openclaw_eval_bridge_lib.mjs"
LIB_URL = LIB_PATH.as_uri()


def _node_available() -> bool:
    return shutil.which("node") is not None


pytestmark = pytest.mark.skipif(
    not _node_available(),
    reason="node is required for bridge dispatcher tests",
)


def _run_dispatch(stub_factory_js: str, params_json: str) -> dict:
    """Execute dispatchEngineImport with a stub engine and return the
    captured calls + dispatcher result.

    `stub_factory_js` is a JS expression returning an engine-shaped object
    that records calls into a top-level `calls` array.
    """
    script = (
        f'import {{ dispatchEngineImport }} from "{LIB_URL}";\n'
        'const calls = [];\n'
        f'const engine = {stub_factory_js};\n'
        f'const params = {params_json};\n'
        'const result = await dispatchEngineImport(engine, params);\n'
        'console.log(JSON.stringify({calls, result}));\n'
    )
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"node script failed (exit {proc.returncode}):\n"
            f"stdout: {proc.stdout}\n"
            f"stderr: {proc.stderr}"
        )
    lines = [l for l in proc.stdout.split("\n") if l.strip()]
    assert lines, f"empty stdout from node:\n{proc.stdout}"
    return json.loads(lines[-1])


_THREE_MESSAGES = json.dumps([
    {"role": "user", "content": [{"type": "text", "text": "msg-1"}]},
    {"role": "assistant", "content": [{"type": "text", "text": "msg-2"}]},
    {"role": "user", "content": [{"type": "text", "text": "msg-3"}]},
])


_BASE_PARAMS = (
    '{'
    '"sessionId": "conv_42",'
    '"sessionFile": "",'
    f'"messages": {_THREE_MESSAGES},'
    '"prePromptMessageCount": 0'
    '}'
)


# --- afterTurn precedence --------------------------------------------------

class TestAfterTurnPrecedence:
    """When engine implements afterTurn, only that method is called."""

    def test_afterTurn_only(self):
        stub = (
            '({'
            '  info: {id: "stub", name: "stub"},'
            '  afterTurn: async (p) => { calls.push({m: "afterTurn", p}); }'
            '})'
        )
        out = _run_dispatch(stub, _BASE_PARAMS)
        methods = [c["m"] for c in out["calls"]]
        assert methods == ["afterTurn"]
        assert out["result"]["method_used"] == "afterTurn"

    def test_afterTurn_chosen_over_ingestBatch(self):
        """Both afterTurn and ingestBatch present: afterTurn wins."""
        stub = (
            '({'
            '  info: {id: "stub", name: "stub"},'
            '  afterTurn: async (p) => { calls.push({m: "afterTurn"}); },'
            '  ingestBatch: async (p) => { calls.push({m: "ingestBatch"}); },'
            '  ingest: async (p) => { calls.push({m: "ingest"}); }'
            '})'
        )
        out = _run_dispatch(stub, _BASE_PARAMS)
        assert [c["m"] for c in out["calls"]] == ["afterTurn"]

    def test_afterTurn_receives_full_message_list(self):
        stub = (
            '({'
            '  info: {id: "stub", name: "stub"},'
            '  afterTurn: async (p) => { calls.push({'
            '    sessionId: p.sessionId,'
            '    msgCount: p.messages.length,'
            '    prePromptMessageCount: p.prePromptMessageCount'
            '  }); }'
            '})'
        )
        out = _run_dispatch(stub, _BASE_PARAMS)
        c = out["calls"][0]
        assert c["sessionId"] == "conv_42"
        assert c["msgCount"] == 3
        assert c["prePromptMessageCount"] == 0


# --- ingestBatch precedence -----------------------------------------------

class TestIngestBatchPrecedence:
    """When engine has ingestBatch but no afterTurn, ingestBatch is used."""

    def test_ingestBatch_only(self):
        stub = (
            '({'
            '  info: {id: "stub", name: "stub"},'
            '  ingestBatch: async (p) => { calls.push({m: "ingestBatch", n: p.messages.length}); },'
            '  ingest: async (p) => { calls.push({m: "ingest"}); }'
            '})'
        )
        out = _run_dispatch(stub, _BASE_PARAMS)
        assert [c["m"] for c in out["calls"]] == ["ingestBatch"]
        assert out["calls"][0]["n"] == 3
        assert out["result"]["method_used"] == "ingestBatch"

    def test_ingestBatch_chosen_over_ingest(self):
        stub = (
            '({'
            '  info: {id: "stub", name: "stub"},'
            '  ingestBatch: async (p) => { calls.push({m: "ingestBatch"}); },'
            '  ingest: async (p) => { calls.push({m: "ingest"}); }'
            '})'
        )
        out = _run_dispatch(stub, _BASE_PARAMS)
        assert [c["m"] for c in out["calls"]] == ["ingestBatch"]


# --- per-message ingest fallback ------------------------------------------

class TestPerMessageIngestFallback:
    """Last resort: per-message ingest, called once per message."""

    def test_ingest_called_per_message(self):
        stub = (
            '({'
            '  info: {id: "stub", name: "stub"},'
            '  ingest: async (p) => { calls.push({'
            '    m: "ingest",'
            '    text: (p.message.content && p.message.content[0] && p.message.content[0].text) || null'
            '  }); }'
            '})'
        )
        out = _run_dispatch(stub, _BASE_PARAMS)
        methods = [c["m"] for c in out["calls"]]
        assert methods == ["ingest", "ingest", "ingest"]
        texts = [c["text"] for c in out["calls"]]
        assert texts == ["msg-1", "msg-2", "msg-3"]
        assert out["result"]["method_used"] == "ingest"

    def test_ingest_passes_through_session_id(self):
        stub = (
            '({'
            '  info: {id: "stub", name: "stub"},'
            '  ingest: async (p) => { calls.push({sessionId: p.sessionId}); }'
            '})'
        )
        out = _run_dispatch(stub, _BASE_PARAMS)
        assert all(c["sessionId"] == "conv_42" for c in out["calls"])


# --- dispatcher result shape ----------------------------------------------

class TestDispatcherResultShape:
    def test_result_includes_message_count(self):
        stub = (
            '({'
            '  info: {id: "stub", name: "stub"},'
            '  afterTurn: async () => {}'
            '})'
        )
        out = _run_dispatch(stub, _BASE_PARAMS)
        assert out["result"]["ok"] is True
        assert out["result"]["message_count"] == 3
        assert out["result"]["method_used"] == "afterTurn"

    def test_no_method_available_raises(self):
        """Engine with neither afterTurn, ingestBatch, nor ingest is invalid."""
        stub_factory = (
            '({ info: {id: "broken", name: "broken"} })'
        )
        # This should raise; we expect node script to exit non-zero
        script = (
            f'import {{ dispatchEngineImport }} from "{LIB_URL}";\n'
            f'const engine = {stub_factory};\n'
            f'const params = {_BASE_PARAMS};\n'
            'try {\n'
            '  await dispatchEngineImport(engine, params);\n'
            '  console.log(JSON.stringify({result: "no-throw"}));\n'
            '} catch (e) {\n'
            '  console.log(JSON.stringify({error: String(e.message || e)}));\n'
            '}\n'
        )
        proc = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            capture_output=True,
            text=True,
            timeout=15,
        )
        lines = [l for l in proc.stdout.split("\n") if l.strip()]
        assert lines
        out = json.loads(lines[-1])
        assert "error" in out
        assert "afterTurn" in out["error"] or "ingest" in out["error"]


# --- empty-message edge case ----------------------------------------------

class TestEmptyMessages:
    def test_empty_messages_no_crash_with_afterTurn(self):
        """afterTurn with empty msg list is still called (engine decides)."""
        stub = (
            '({'
            '  info: {id: "stub", name: "stub"},'
            '  afterTurn: async (p) => { calls.push({n: p.messages.length}); }'
            '})'
        )
        params = (
            '{"sessionId": "c", "sessionFile": "",'
            ' "messages": [], "prePromptMessageCount": 0}'
        )
        out = _run_dispatch(stub, params)
        assert out["calls"][0]["n"] == 0

    def test_empty_messages_skips_ingestBatch(self):
        """No new messages: ingestBatch should not be called (avoid empty batch)."""
        stub = (
            '({'
            '  info: {id: "stub", name: "stub"},'
            '  ingestBatch: async (p) => { calls.push({m: "ingestBatch"}); }'
            '})'
        )
        params = (
            '{"sessionId": "c", "sessionFile": "",'
            ' "messages": [], "prePromptMessageCount": 0}'
        )
        out = _run_dispatch(stub, params)
        # Engine had no afterTurn, has ingestBatch — but no new messages.
        # Production code skips the call (attempt.context-engine-helpers.ts:127).
        assert out["calls"] == []
        assert out["result"]["method_used"] == "ingestBatch_skipped"


# --- bridge handler integration (no launcher = stub mode) -----------------

class TestBridgeHandlerStubMode:
    """End-to-end: drive the bridge mjs binary with no launcher.

    The handler should return a deterministic stub-shaped response that
    Python adapters can rely on for contract testing without docker.
    """

    BRIDGE_PATH = REPO_ROOT / "evaluation" / "scripts" / "openclaw_eval_bridge.mjs"

    def _call_bridge(self, payload: dict, env_overrides: dict | None = None) -> dict:
        """Spawn the bridge with stdin=payload, no OPENCLAW_REPO_PATH."""
        import os

        env = {k: v for k, v in os.environ.items() if k != "OPENCLAW_REPO_PATH"}
        if env_overrides:
            env.update(env_overrides)
        proc = subprocess.run(
            ["node", str(self.BRIDGE_PATH)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        if proc.returncode != 0:
            pytest.fail(
                f"bridge failed (exit {proc.returncode}):\n"
                f"stdout: {proc.stdout}\n"
                f"stderr: {proc.stderr}"
            )
        return json.loads(proc.stdout.strip().split("\n")[-1])

    def test_stub_mode_returns_ok(self):
        """No launcher -> ok=true, native=false, method_used=stub."""
        resp = self._call_bridge({
            "command": "engine_import_history",
            "session_id": "conv_locomo_0",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "hi"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
            ],
        })
        assert resp["ok"] is True
        assert resp["command"] == "engine_import_history"
        assert resp["native"] is False
        assert resp["method_used"] == "stub"
        assert resp["message_count"] == 2
        assert resp["session_id"] == "conv_locomo_0"

    def test_stub_mode_handles_missing_messages(self):
        resp = self._call_bridge({
            "command": "engine_import_history",
            "session_id": "c",
        })
        assert resp["ok"] is True
        assert resp["message_count"] == 0
