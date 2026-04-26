"""
v0.7 D2 unit tests for openclaw_eval_bridge_lib.mjs helpers.

Tests stripAnsi and extractJsonObject by spawning a small Node ESM
test runner that imports the lib and asserts behaviors. The lib is
pure (no I/O) so subprocess is appropriate.

Why subprocess (not direct JS test runner): evermemos doesn't pin a
JS test framework; reusing the existing python+node bridge plumbing
keeps test infra minimal and consistent with test_openclaw_runtime.py.
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
    reason="node is required for bridge lib tests",
)


def _run_js(script: str) -> dict:
    """Execute a small Node ESM script that imports the lib.

    Script convention: must console.log() a single JSON object as the
    last line. Returns parsed JSON.
    """
    full_script = (
        f'import {{ stripAnsi, extractJsonObject }} from "{LIB_URL}";\n'
        + script
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", full_script],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        pytest.fail(
            f"node test script failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
    # Last non-empty stdout line should be JSON
    lines = [l for l in result.stdout.split("\n") if l.strip()]
    assert lines, f"empty stdout from node script:\n{result.stdout}"
    return json.loads(lines[-1])


# --- stripAnsi --------------------------------------------------------

def test_strip_ansi_strips_real_escape_sequences():
    """ANSI escape with ESC byte (0x1b) is stripped."""
    out = _run_js(
        'const r = stripAnsi("\\x1b[31mERROR\\x1b[0m");\n'
        'console.log(JSON.stringify({result: r}));'
    )
    assert out["result"] == "ERROR"


def test_strip_ansi_does_not_eat_literal_brackets():
    """Plain '[33m' (no ESC prefix) in reply text is preserved.

    This is the critical bug from Codex round-6: a regex without \\x1b
    would eat any '[xx]'-shaped substring, including legitimate text
    like markdown brackets or example ANSI strings in docs.
    """
    out = _run_js(
        'const r = stripAnsi("text with [33m] and [0;1m brackets in it");\n'
        'console.log(JSON.stringify({result: r}));'
    )
    assert out["result"] == "text with [33m] and [0;1m brackets in it"


def test_strip_ansi_handles_empty_input():
    out = _run_js(
        'console.log(JSON.stringify({'
        'a: stripAnsi(""), '
        'b: stripAnsi(null), '
        'c: stripAnsi(undefined)'
        '}));'
    )
    assert out["a"] == ""
    assert out["b"] == ""
    assert out["c"] == ""


def test_strip_ansi_mixed_content():
    """Stripping ANSI in mixed content keeps surrounding text intact."""
    out = _run_js(
        'const r = stripAnsi("\\x1b[33m[plugins]\\x1b[0m loaded ok");\n'
        'console.log(JSON.stringify({result: r}));'
    )
    assert out["result"] == "[plugins] loaded ok"


# --- extractJsonObject -----------------------------------------------

def test_extract_json_simple_block():
    """Plain JSON block at end of stderr."""
    out = _run_js(
        'const text = `\n'
        '[plugins] some warning\n'
        '{\n'
        '  "payloads": [{"text": "HELLO", "mediaUrl": null}],\n'
        '  "meta": {"durationMs": 100, "stopReason": "stop"}\n'
        '}\n'
        '`;\n'
        'const r = extractJsonObject(text);\n'
        'console.log(JSON.stringify({reply: r.payloads[0].text}));'
    )
    assert out["reply"] == "HELLO"


def test_extract_json_ignores_prefix_jsonlike_blocks():
    """Prefix JSON-like blocks (e.g. plugin warning) are ignored."""
    out = _run_js(
        'const text = `\n'
        '[plugins] some warning\n'
        '{"level": "error", "code": 42, "msg": "plugin failed"}\n'
        '{"level": "info", "msg": "another inline"}\n'
        '{\n'
        '  "payloads": [{"text": "FINAL", "mediaUrl": null}],\n'
        '  "meta": {"durationMs": 200, "stopReason": "stop"}\n'
        '}\n'
        '`;\n'
        'const r = extractJsonObject(text);\n'
        'console.log(JSON.stringify({reply: r.payloads[0].text}));'
    )
    assert out["reply"] == "FINAL"


def test_extract_json_handles_curly_in_reply():
    """Reply text containing { or } characters does not break extraction.

    This is the second critical bug from Codex round-6: a manual brace
    counter would mis-balance when reply has curlies.
    """
    out = _run_js(
        r'''
const text = `
{
  "payloads": [{"text": "use {x} as {y} in this {pattern}", "mediaUrl": null}],
  "meta": {"durationMs": 50, "stopReason": "stop"}
}
`;
const r = extractJsonObject(text);
console.log(JSON.stringify({reply: r.payloads[0].text}));
'''
    )
    assert out["reply"] == "use {x} as {y} in this {pattern}"


def test_extract_json_returns_null_without_payloads_meta():
    """JSON without required schema keys returns null."""
    out = _run_js(
        'const text = `{"some": "other", "shape": true}`;\n'
        'const r = extractJsonObject(text);\n'
        'console.log(JSON.stringify({result: r}));'
    )
    assert out["result"] is None


def test_extract_json_returns_null_for_empty_input():
    out = _run_js(
        'console.log(JSON.stringify({'
        'a: extractJsonObject(""), '
        'b: extractJsonObject(null), '
        'c: extractJsonObject(undefined)'
        '}));'
    )
    assert out["a"] is None
    assert out["b"] is None
    assert out["c"] is None


def test_extract_json_picks_last_valid_block_when_multiple_present():
    """Multiple complete payloads+meta blocks: takes the last (most
    recent run output)."""
    out = _run_js(
        'const text = `\n'
        '{\n'
        '  "payloads": [{"text": "FIRST", "mediaUrl": null}],\n'
        '  "meta": {"stopReason": "stop"}\n'
        '}\n'
        'some intermediate noise\n'
        '{\n'
        '  "payloads": [{"text": "SECOND", "mediaUrl": null}],\n'
        '  "meta": {"stopReason": "stop"}\n'
        '}\n'
        '`;\n'
        'const r = extractJsonObject(text);\n'
        'console.log(JSON.stringify({reply: r.payloads[0].text}));'
    )
    assert out["reply"] == "SECOND"


def test_extract_json_d1_real_stderr_pattern():
    """Reproduce the structure of D1's actual openclaw stderr (warning
    lines + indented JSON block)."""
    out = _run_js(
        r'''
const text = `[plugins] acpx failed to load from /Data3/.../dist-runtime/extensions/acpx/index.js: Error: Cannot find module 'acpx/runtime'
Require stack:
- /Data3/.../dist/register.runtime-Cn6W3s44.js
[plugins] acpx failed to load from /Data3/.../dist-runtime/extensions/acpx/index.js: Error: Cannot find module 'acpx/runtime'
Require stack:
- /Data3/.../dist/register.runtime-Cn6W3s44.js
{
  "payloads": [
    {
      "text": "ECHO",
      "mediaUrl": null
    }
  ],
  "meta": {
    "durationMs": 2796,
    "agentMeta": {
      "sessionId": "check1_run",
      "provider": "sophnet",
      "model": "gpt-4.1-mini"
    },
    "aborted": false,
    "stopReason": "stop"
  }
}
`;
const r = extractJsonObject(text);
console.log(JSON.stringify({
  reply: r.payloads[0].text,
  sessionId: r.meta.agentMeta.sessionId,
  stopReason: r.meta.stopReason
}));
'''
    )
    assert out["reply"] == "ECHO"
    assert out["sessionId"] == "check1_run"
    assert out["stopReason"] == "stop"
