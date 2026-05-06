"""Regression test for Form B sidecar routing in the bridge files.

Both `evaluation/scripts/openclaw_eval_bridge.mjs` (canonical, source of
truth) and `openclaw-eval/container/openclaw_eval_bridge.mjs` (mirror baked
into Docker images) must short-circuit handleIndex/handleStatus through
the sidecar HTTP API when /sidecar/server.py exists in the runtime.
A previous bridge sync silently dropped this routing, causing
mem0/evermemos/zep ingestion to invoke `openclaw memory index --force`
which fails because memory-core is disabled in those Form B images.

Static structural test rather than a Node subprocess test: lets us guard
against future bridge syncs that re-drop the routing without requiring
test-time Node + a fake sidecar.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_FILES = [
    REPO_ROOT / "evaluation" / "scripts" / "openclaw_eval_bridge.mjs",
    REPO_ROOT / "openclaw-eval" / "container" / "openclaw_eval_bridge.mjs",
]


@pytest.fixture(scope="module")
def bridge_texts() -> dict[Path, str]:
    """Read each bridge once per test session — these files are ~600 lines
    and every test in this module reads the same content."""
    return {p: p.read_text() for p in BRIDGE_FILES}


def _extract_function_body(text: str, func_name: str) -> str:
    match = re.search(
        rf"async function {func_name}\s*\([^)]*\)\s*\{{(.*?)\n\}}\n",
        text,
        re.DOTALL,
    )
    assert match is not None, f"{func_name} not found"
    return match.group(1)


@pytest.mark.parametrize("bridge", BRIDGE_FILES, ids=lambda p: p.parent.name)
class TestSidecarRouting:
    def test_sidecar_constants_present(self, bridge: Path, bridge_texts) -> None:
        text = bridge_texts[bridge]
        assert 'SIDECAR_SCRIPT = "/sidecar/server.py"' in text
        assert "SIDECAR_BASE_URL" in text
        assert "SIDECAR_INDEX_TIMEOUT_MS" in text

    def test_has_sidecar_helper_defined(self, bridge: Path, bridge_texts) -> None:
        assert re.search(r"function hasSidecar\s*\(", bridge_texts[bridge]), \
            "hasSidecar() helper missing from bridge"

    @pytest.mark.parametrize(
        "handler,cli_marker,sidecar_helper",
        [
            ("handleIndex",  '"memory", "index"',  "handleIndexViaSidecar"),
            ("handleStatus", '"memory", "status"', "handleStatusViaSidecar"),
        ],
    )
    def test_handler_short_circuits_via_sidecar(
        self,
        bridge: Path,
        bridge_texts,
        handler: str,
        cli_marker: str,
        sidecar_helper: str,
    ) -> None:
        """handleIndex/handleStatus must check hasSidecar() and route to the
        sidecar helper before falling through to the openclaw CLI."""
        body = _extract_function_body(bridge_texts[bridge], handler)
        sidecar_pos = body.find("hasSidecar()")
        cli_pos = body.find(cli_marker)
        assert sidecar_pos != -1, f"{handler} never checks hasSidecar()"
        assert cli_pos != -1, f"openclaw CLI invocation not found in {handler}"
        assert sidecar_pos < cli_pos, \
            "hasSidecar() short-circuit must come before the openclaw CLI path"
        assert sidecar_helper in body, \
            f"{handler} doesn't dispatch to {sidecar_helper}"

    def test_sidecar_index_posts_to_sync_endpoint(
        self, bridge: Path, bridge_texts
    ) -> None:
        body = _extract_function_body(bridge_texts[bridge], "handleIndexViaSidecar")
        assert '"POST"' in body
        assert '"/sync"' in body
        assert "session_files" in body

    def test_readdirSync_imported_for_sidecar_walk(
        self, bridge: Path, bridge_texts
    ) -> None:
        """handleIndexViaSidecar walks <workspace>/memory/*.md via readdirSync."""
        assert re.search(
            r"import\s*\{[^}]*readdirSync[^}]*\}\s*from\s*\"node:fs\"",
            bridge_texts[bridge],
        ), "readdirSync not imported from node:fs"

    def test_sidecar_index_propagates_logical_failures(
        self, bridge: Path, bridge_texts
    ) -> None:
        """The sidecar returns HTTP 200 even on logical failures
        (e.g. force-reset failure surfaces as `{ok:false, error:...}`).
        handleIndexViaSidecar must inspect data.ok and propagate the
        failure as a bridge-level index error; otherwise the pipeline
        continues with stale/duplicated memory state."""
        body = _extract_function_body(bridge_texts[bridge], "handleIndexViaSidecar")
        ok_check_pos = body.find("data.ok === false")
        success_return_pos = body.find("ok: true")
        assert ok_check_pos != -1, (
            "handleIndexViaSidecar does not inspect data.ok — sidecar "
            "logical failures will be masked as successful index ops"
        )
        assert success_return_pos != -1
        assert ok_check_pos < success_return_pos, (
            "data.ok check must come before the ok:true return"
        )
        assert "data.error" in body, (
            "sidecar's error message should be included in the bridge response"
        )
