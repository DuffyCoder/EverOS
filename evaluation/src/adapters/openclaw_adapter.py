"""
OpenClaw adapter for the EverMemOS evaluation pipeline.

Wraps OpenClaw memory lifecycle (ingest / flush / index / search / get) via a
Node bridge, exposes the BaseAdapter surface, and emits session-level
retrieval traces + lifecycle diagnostics alongside the shared answer prompt.

Task 4 adds add() + build_lazy_index() with per-conversation sandbox
isolation. ingest + flush calls are currently thin placeholders so the
control flow is exercisable without a real OpenClaw runtime; Task 8 wires
them to the native bridge.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, List, Optional

from evaluation.src.adapters.base import BaseAdapter
from evaluation.src.adapters.openclaw_manifest import build_session_manifest
from evaluation.src.adapters.openclaw_runtime import build_sandbox_paths
from evaluation.src.adapters.registry import register_adapter
from evaluation.src.core.data_models import Conversation, SearchResult


logger = logging.getLogger(__name__)


_RUN_ID_LATEST_FILE = "LATEST"
_ARTIFACT_ROOT = "artifacts/openclaw"


@register_adapter("openclaw")
class OpenClawAdapter(BaseAdapter):
    def __init__(self, config: dict, output_dir: Any = None):
        super().__init__(config)
        self.output_dir = output_dir
        self._prepared: bool = False
        self._run_id: Optional[str] = None
        self._openclaw_cfg: dict = dict(config.get("openclaw") or {})

    # ----------------------------------------------------------------- prepare
    async def prepare(
        self,
        conversations: List[Conversation],
        output_dir: Any = None,
        checkpoint_manager: Any = None,
        **kwargs,
    ) -> None:
        """Idempotent initialization.

        Pipeline currently doesn't call prepare() explicitly, so add() calls it
        internally. When a future pipeline wires prepare() in, this flag keeps
        initialization from running twice.
        """
        if self._prepared:
            return
        self._prepared = True
        self._prepared_conversation_ids = [c.conversation_id for c in conversations]
        logger.debug(
            "openclaw adapter prepared for %d conversations",
            len(self._prepared_conversation_ids),
        )

    # --------------------------------------------------------------------- add
    async def add(
        self,
        conversations: List[Conversation],
        output_dir: Any = None,
        checkpoint_manager: Any = None,
        **kwargs,
    ) -> dict:
        if not self._prepared:
            await self.prepare(
                conversations=conversations,
                output_dir=output_dir,
                checkpoint_manager=checkpoint_manager,
                **kwargs,
            )

        root_dir = self._resolve_run_root(output_dir or self.output_dir)
        run_id = root_dir.name
        conversations_map: dict[str, dict] = {}

        for conv in conversations:
            sandbox = self._prepare_conversation_sandbox(root_dir, conv)
            t0 = time.perf_counter()
            try:
                await self._ingest_conversation(sandbox, conv)
                await self._flush_and_settle_if_needed(sandbox)
            except Exception as err:
                sandbox["run_status"] = "failed"
                self._write_handle(sandbox, add_summary={"error": str(err)})
                logger.exception("openclaw ingest failed for %s", conv.conversation_id)
                raise
            add_latency_ms = (time.perf_counter() - t0) * 1000.0
            sandbox["run_status"] = "ready"
            sandbox["visibility_state"] = "settled"
            self._write_handle(
                sandbox,
                add_summary={
                    "conversation_id": conv.conversation_id,
                    "add_latency_ms": add_latency_ms,
                },
            )
            conversations_map[conv.conversation_id] = sandbox

        return {
            "type": "openclaw_sandboxes",
            "run_id": run_id,
            "root_dir": str(root_dir),
            "conversations": conversations_map,
        }

    # --------------------------------------------------------- build_lazy_index
    def build_lazy_index(
        self, conversations: List[Conversation], output_dir: Any
    ) -> dict:
        root_dir = self._locate_existing_run_root(Path(output_dir))
        handles: dict[str, dict] = {}
        for conv in conversations:
            handle_path = root_dir / "conversations" / conv.conversation_id / "handle.json"
            if not handle_path.exists():
                continue
            handle = json.loads(handle_path.read_text())
            if (
                handle.get("run_status") != "ready"
                or handle.get("visibility_state") != "settled"
            ):
                continue
            handles[conv.conversation_id] = handle
        return {
            "type": "openclaw_sandboxes",
            "run_id": root_dir.name,
            "root_dir": str(root_dir),
            "conversations": handles,
        }

    # ---------------------------------------------------------------- search
    async def search(
        self, query: str, conversation_id: str, index: Any, **kwargs
    ) -> SearchResult:
        raise NotImplementedError("search() is implemented in Task 5")

    # ----------------------------------------------------------- system info
    def get_system_info(self) -> dict:
        return {"name": "OpenClaw", "config": self.config}

    # ===================================================== internal helpers
    def _resolve_run_root(self, output_dir: Any) -> Path:
        if output_dir is None:
            raise ValueError("output_dir is required to resolve openclaw sandbox root")
        if self._run_id is None:
            self._run_id = time.strftime("run-%Y%m%dT%H%M%S")
        root = Path(output_dir) / _ARTIFACT_ROOT / self._run_id
        root.mkdir(parents=True, exist_ok=True)
        (Path(output_dir) / _ARTIFACT_ROOT / _RUN_ID_LATEST_FILE).write_text(self._run_id)
        return root

    def _locate_existing_run_root(self, output_dir: Path) -> Path:
        latest_file = output_dir / _ARTIFACT_ROOT / _RUN_ID_LATEST_FILE
        if latest_file.exists():
            run_id = latest_file.read_text().strip()
            root = output_dir / _ARTIFACT_ROOT / run_id
            if root.exists():
                return root
        # Fallback: newest run directory by mtime
        parent = output_dir / _ARTIFACT_ROOT
        if not parent.exists():
            raise FileNotFoundError(f"no openclaw artifacts under {parent}")
        runs = [p for p in parent.iterdir() if p.is_dir()]
        if not runs:
            raise FileNotFoundError(f"no openclaw runs under {parent}")
        runs.sort(key=lambda p: p.stat().st_mtime)
        return runs[-1]

    def _prepare_conversation_sandbox(
        self, root_dir: Path, conv: Conversation
    ) -> dict:
        run_id = root_dir.name
        output_dir = root_dir.parent.parent.parent  # strip artifacts/openclaw/<run>
        paths = build_sandbox_paths(output_dir, run_id, conv.conversation_id)

        base = Path(paths["base_dir"])
        base.mkdir(parents=True, exist_ok=True)
        Path(paths["native_store_dir"]).mkdir(parents=True, exist_ok=True)
        Path(paths["metrics_dir"]).mkdir(parents=True, exist_ok=True)
        Path(paths["events_path"]).touch(exist_ok=True)

        manifest = build_session_manifest(
            conv, dataset_name=self.config.get("dataset_name", "")
        )
        manifest_path = base / "session_manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))

        resolved_config_path = base / "openclaw.resolved.json"
        resolved_config_path.write_text(
            json.dumps(self._openclaw_cfg, ensure_ascii=False, indent=2)
        )

        handle: dict = {
            "conversation_id": conv.conversation_id,
            "workspace_dir": paths["workspace_dir"],
            "native_store_dir": paths["native_store_dir"],
            "resolved_config_path": str(resolved_config_path),
            "session_manifest_path": str(manifest_path),
            "prov_units_path": str(base / "prov_units.jsonl"),
            "artifact_bindings_path": str(base / "artifact_bindings.jsonl"),
            "events_path": paths["events_path"],
            "metrics_dir": paths["metrics_dir"],
            "backend_mode": self._openclaw_cfg.get("backend_mode", "hybrid"),
            "retrieval_route": self._openclaw_cfg.get(
                "retrieval_route", "search_then_get"
            ),
            "visibility_mode": self._openclaw_cfg.get("visibility_mode", "settled"),
            "visibility_state": "prepared",
            "run_status": "pending",
            "last_flush_epoch": 0,
            "last_index_epoch": 0,
            "retrieval_eval_supported": True,
        }
        return handle

    def _write_handle(self, handle: dict, add_summary: Optional[dict] = None) -> None:
        base = Path(handle["workspace_dir"])
        handle_path = base / "handle.json"
        handle_path.write_text(json.dumps(handle, ensure_ascii=False, indent=2))

        if add_summary is not None:
            (Path(handle["metrics_dir"]) / "add_summary.json").write_text(
                json.dumps(add_summary, ensure_ascii=False, indent=2)
            )

    async def _ingest_conversation(self, sandbox: dict, conv: Conversation) -> None:
        """Ingest raw transcript into the OpenClaw sandbox.

        Placeholder - Task 8 wires this to the native ``index`` bridge command
        once OpenClaw CLI is available. Tests monkeypatch this method.
        """
        sandbox["visibility_state"] = "ingested"

    async def _flush_and_settle_if_needed(self, sandbox: dict) -> None:
        """Run flush + wait for index settle if visibility_mode == 'settled'.

        Placeholder - Task 8 wires this to the ``flush`` / ``status`` bridge
        commands. Tests monkeypatch this method.
        """
        sandbox["visibility_state"] = "indexed"
