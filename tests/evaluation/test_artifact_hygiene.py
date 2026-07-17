from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest


def test_artifact_hygiene_package_is_available() -> None:
    assert importlib.util.find_spec("evaluation.tools.artifact_hygiene") is not None


def _inventory_module():
    module_name = "evaluation.tools.artifact_hygiene.inventory"
    assert importlib.util.find_spec(module_name) is not None
    return importlib.import_module(module_name)


def _archive_module():
    module_name = "evaluation.tools.artifact_hygiene.archive"
    assert importlib.util.find_spec(module_name) is not None
    return importlib.import_module(module_name)


@pytest.mark.parametrize(
    ("name", "flags", "decision", "reason"),
    [
        (
            "debug-run",
            {
                "referenced": True,
                "pinned": True,
                "successful": True,
                "is_latest_success": True,
            },
            "keep",
            "pinned",
        ),
        (
            "debug-run",
            {
                "referenced": True,
                "pinned": False,
                "successful": True,
                "is_latest_success": True,
            },
            "keep",
            "referenced",
        ),
        (
            "debug-run",
            {
                "referenced": False,
                "pinned": False,
                "successful": True,
                "is_latest_success": True,
            },
            "keep",
            "latest_success",
        ),
        (
            "SMOKE-Run",
            {
                "referenced": False,
                "pinned": False,
                "successful": False,
                "is_latest_success": False,
            },
            "delete_candidate",
            "name_marker:smoke",
        ),
        (
            "ordinary-unconfirmed-run",
            {
                "referenced": False,
                "pinned": False,
                "successful": False,
                "is_latest_success": False,
            },
            "review",
            "manual_review",
        ),
    ],
)
def test_classify_result_uses_safe_precedence(
    name: str, flags: dict[str, bool], decision: str, reason: str
) -> None:
    result = _inventory_module().classify_result(name, **flags)

    assert result["decision"] == decision
    assert result["reasons"][0] == reason
    json.dumps(result)


@pytest.mark.parametrize(
    "marker", ["smoke", "debug", "retry", "calib", "test", "incomplete"]
)
def test_classify_result_recognizes_disposable_markers_case_insensitively(
    marker: str,
) -> None:
    result = _inventory_module().classify_result(
        f"prefix-{marker.upper()}-suffix",
        referenced=False,
        pinned=False,
        successful=False,
        is_latest_success=False,
    )

    assert result == {
        "decision": "delete_candidate",
        "reasons": [f"name_marker:{marker}"],
    }


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_build_inventory_reports_metadata_and_conservative_decisions(
    tmp_path: Path,
) -> None:
    inventory_module = _inventory_module()
    assert hasattr(inventory_module, "build_inventory")

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    results_root = tmp_path / "evaluation" / "results"
    old_result = results_root / "ordinary-old"
    new_result = results_root / "ordinary-new"
    pinned_result = results_root / "debug-run"
    unknown_result = results_root / "plain-unknown"
    retry_result = results_root / "retry-unused"

    old_metrics = _write(old_result / "metrics.json", '{"score": 1}\n')
    new_metrics = _write(new_result / "summary.json", '{"score": 2}\n')
    _write(pinned_result / "notes.txt", "debug evidence\n")
    _write(unknown_result / "raw.bin", "ordinary\n")
    _write(retry_result / "raw.bin", "retry\n")
    os.utime(old_metrics, (1, 1))
    os.utime(old_result, (1, 1))
    os.utime(new_metrics, (2, 2))
    os.utime(new_result, (2, 2))

    outside = _write(tmp_path / "outside-secret.txt", "must not be counted\n")
    (new_result / "linked-secret").symlink_to(outside)

    keep_file = tmp_path / "evaluation" / "archives" / "KEEP"
    _write(
        keep_file,
        "# Explicit operator pin\n\n"
        "evaluation/results/debug-run\n"
        "./evaluation/results/debug-run\n",
    )
    _write(
        tmp_path / "evaluation" / "archives" / "archived-run" / "manifest.json", "{}\n"
    )
    _write(
        tmp_path / "docs" / "evaluation" / "analysis" / "report.md",
        "Evidence: evaluation/results/ordinary-old\n",
    )

    root_backup = _write(tmp_path / ".env.bak.secret", "ROOT_SECRET=value\n")
    os.chmod(root_backup, 0)
    _write(tmp_path / "service" / ".env.bak.1", "SHALLOW_SECRET=value\n")
    _write(tmp_path / "service" / "nested" / ".env.bak.deep", "DEEP_SECRET=value\n")

    inventory = inventory_module.build_inventory(tmp_path)

    assert inventory["schema"] == "evaluation-artifact-inventory/v1"
    assert inventory["generated_at"].endswith("Z")
    assert inventory["repo_root"] == str(tmp_path.resolve())
    assert inventory["worktree_error"] is None
    assert inventory["registered_worktrees"][0]["path"] == str(tmp_path.resolve())
    assert inventory["keep_entries"] == ["evaluation/results/debug-run"]

    backups = {entry["path"]: entry for entry in inventory["environment_backups"]}
    assert set(backups) == {".env.bak.secret", "service/.env.bak.1"}
    assert set(backups[".env.bak.secret"]) == {"path", "size", "mode", "mtime", "type"}
    assert backups[".env.bak.secret"]["mode"] == stat.S_IMODE(
        root_backup.lstat().st_mode
    )
    serialized = json.dumps(inventory, sort_keys=True)
    assert "ROOT_SECRET" not in serialized
    assert "SHALLOW_SECRET" not in serialized

    results = {entry["name"]: entry for entry in inventory["results"]}
    assert results["ordinary-old"]["successful"] is True
    assert results["ordinary-old"]["referenced"] is True
    assert results["ordinary-old"]["decision"] == "keep"
    assert results["ordinary-new"]["successful"] is True
    assert results["ordinary-new"]["is_latest_success"] is False
    assert results["ordinary-new"]["decision"] == "review"
    assert results["debug-run"]["pinned"] is True
    assert results["debug-run"]["reasons"] == ["pinned"]
    assert results["plain-unknown"]["decision"] == "review"
    assert results["retry-unused"]["decision"] == "delete_candidate"
    assert results["ordinary-new"]["size"] == len(new_metrics.read_bytes())

    assert [entry["path"] for entry in inventory["archives"]] == [
        "evaluation/archives/archived-run"
    ]
    assert [entry["path"] for entry in inventory["analysis_files"]] == [
        "docs/evaluation/analysis/report.md"
    ]


def test_build_inventory_safely_reports_git_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory_module = _inventory_module()
    assert hasattr(inventory_module, "build_inventory")
    monkeypatch.setenv("PATH", "")

    inventory = inventory_module.build_inventory(tmp_path)

    assert inventory["registered_worktrees"] == []
    assert inventory["worktree_error"]


@pytest.mark.parametrize("symlink_ancestor", [False, True])
def test_build_inventory_does_not_follow_symlinked_anchors_or_ancestors(
    tmp_path: Path, symlink_ancestor: bool
) -> None:
    inventory_module = _inventory_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "external"
    _write(
        external / "evaluation" / "results" / "external-run" / "metrics.json", "{}\n"
    )
    _write(
        external / "evaluation" / "archives" / "external-archive" / "manifest.json",
        "{}\n",
    )
    _write(
        external / "evaluation" / "archives" / "KEEP",
        "evaluation/results/external-run\n",
    )
    _write(
        external / "docs" / "evaluation" / "analysis" / "external.md",
        "EXTERNAL_ANALYSIS_CONTENT\n",
    )

    if symlink_ancestor:
        (repo / "evaluation").symlink_to(
            external / "evaluation", target_is_directory=True
        )
        (repo / "docs").symlink_to(external / "docs", target_is_directory=True)
    else:
        (repo / "evaluation").mkdir()
        (repo / "evaluation" / "results").symlink_to(
            external / "evaluation" / "results", target_is_directory=True
        )
        (repo / "evaluation" / "archives").symlink_to(
            external / "evaluation" / "archives", target_is_directory=True
        )
        (repo / "docs" / "evaluation").mkdir(parents=True)
        (repo / "docs" / "evaluation" / "analysis").symlink_to(
            external / "docs" / "evaluation" / "analysis", target_is_directory=True
        )

    inventory = inventory_module.build_inventory(repo)

    assert inventory["results"] == []
    assert inventory["archives"] == []
    assert inventory["analysis_files"] == []
    assert inventory["keep_entries"] == []
    assert "EXTERNAL_ANALYSIS_CONTENT" not in json.dumps(inventory)


def test_build_inventory_reads_references_only_from_bounded_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory_module = _inventory_module()
    results_root = tmp_path / "evaluation" / "results"
    _write(results_root / "from-small-markdown" / "metrics.json", "{}\n")
    _write(results_root / "from-pdf" / "metrics.json", "{}\n")
    _write(results_root / "past-markdown-limit" / "metrics.json", "{}\n")
    _write(results_root / "debug-past-markdown-limit" / "metrics.json", "{}\n")
    analysis_root = tmp_path / "docs" / "evaluation" / "analysis"
    _write(
        analysis_root / "small.md", "Evidence: evaluation/results/from-small-markdown\n"
    )
    raw_pdf = _write(
        analysis_root / "raw.pdf", "PDF payload mentions evaluation/results/from-pdf\n"
    )
    scan_limit = inventory_module._ANALYSIS_REFERENCE_SCAN_BYTES
    assert 0 < scan_limit <= 1024 * 1024
    _write(
        analysis_root / "oversized.md",
        "x" * scan_limit
        + "\nEvidence: evaluation/results/past-markdown-limit\n"
        + "Evidence: evaluation/results/debug-past-markdown-limit\n",
    )

    original_read_text = Path.read_text

    def guarded_read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == raw_pdf or path.suffix.casefold() == ".md":
            raise AssertionError(
                f"analysis content must use a bounded Markdown reader: {path}"
            )
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    inventory = inventory_module.build_inventory(tmp_path)

    results = {entry["name"]: entry for entry in inventory["results"]}
    assert results["from-small-markdown"]["referenced"] is True
    assert results["from-pdf"]["referenced"] is False
    assert results["past-markdown-limit"]["referenced"] is False
    assert results["debug-past-markdown-limit"]["decision"] == "review"
    assert results["debug-past-markdown-limit"]["reasons"] == [
        "analysis_reference_scan_incomplete"
    ]
    assert {entry["path"] for entry in inventory["analysis_files"]} == {
        "docs/evaluation/analysis/oversized.md",
        "docs/evaluation/analysis/raw.pdf",
        "docs/evaluation/analysis/small.md",
    }
    analyses = {entry["path"]: entry for entry in inventory["analysis_files"]}
    assert (
        analyses["docs/evaluation/analysis/small.md"]["reference_scan"]["status"]
        == "complete"
    )
    assert analyses["docs/evaluation/analysis/raw.pdf"]["reference_scan"] == {
        "status": "not_scanned",
        "reason": "non_markdown",
    }
    assert (
        analyses["docs/evaluation/analysis/oversized.md"]["reference_scan"]["status"]
        == "truncated"
    )


def test_build_inventory_never_opens_environment_backup_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory_module = _inventory_module()
    root_backup = _write(tmp_path / ".env.bak.root", "ROOT_SECRET=value\n")
    shallow_backup = _write(
        tmp_path / "service" / ".env.bak.shallow", "SHALLOW_SECRET=value\n"
    )
    os.chmod(root_backup, 0)

    original_read_text = Path.read_text
    original_open = Path.open

    def guarded_read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path.name.startswith(".env.bak"):
            raise AssertionError(f"environment backup content was read: {path}")
        return original_read_text(path, *args, **kwargs)

    def guarded_open(path: Path, *args: object, **kwargs: object):
        if path.name.startswith(".env.bak"):
            raise AssertionError(f"environment backup content was opened: {path}")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    monkeypatch.setattr(Path, "open", guarded_open)

    inventory = inventory_module.build_inventory(tmp_path)

    assert {entry["path"] for entry in inventory["environment_backups"]} == {
        ".env.bak.root",
        "service/.env.bak.shallow",
    }
    assert "ROOT_SECRET" not in json.dumps(inventory)
    assert "SHALLOW_SECRET" not in json.dumps(inventory)


def test_inventory_cli_writes_only_the_explicit_deterministic_json_output(
    tmp_path: Path,
) -> None:
    output = tmp_path / "reports" / "inventory.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "evaluation.tools.artifact_hygiene",
            "inventory",
            "--repo-root",
            str(tmp_path),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert output.is_file()
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["repo_root"] == str(tmp_path.resolve())
    assert (
        output.read_text(encoding="utf-8")
        == json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )
    assert not (tmp_path / "artifact-inventory.json").exists()


def _make_archive_plan_fixture(tmp_path: Path) -> Path:
    result = tmp_path / "result-run"
    for relative_path in [
        "metrics.json",
        "summary-final.json",
        "benchmark_summary.json",
        "retrieval_metrics.json",
        "answer_aux_metrics.json",
        "diagnostics.json",
        "latency-p95.json",
        "content-overlap.json",
        "resolved_config.yaml",
        "config/resolved-config.json",
        "resolved-system-config.json",
        "report.md",
        "report-details.txt",
        "run.log",
        "answer-01.json",
        "nested/search-trace.json",
        "nested/eval-results.json",
        "logs/server.log",
        "logs/context.txt",
        "artifacts/evidence.csv",
        "artifacts/nested/note.txt",
    ]:
        _write(result / relative_path, f"evidence for {relative_path}\n")

    for relative_path in [
        "random.bin",
        "compiled.pyc",
        "artifacts/blob.bin",
        "nested/ordinary.json",
    ]:
        _write(result / relative_path, f"excluded {relative_path}\n")

    for component in [
        "openclaw-workspaces",
        "node_modules",
        ".cache",
        ".venv",
        "__pycache__",
        "vectors",
        "vector_index",
        "bm25-index",
        "model_cache",
    ]:
        _write(result / component / "must-not-be-visited.json", "large or secret\n")

    outside = _write(tmp_path / "outside.json", "outside\n")
    (result / "linked.json").symlink_to(outside)
    return result


def test_plan_archive_includes_compact_evidence_and_explains_every_exclusion(
    tmp_path: Path,
) -> None:
    archive_module = _archive_module()
    assert hasattr(archive_module, "plan_archive")
    result = _make_archive_plan_fixture(tmp_path)
    archive_root = tmp_path / "archives-not-created"

    plan = archive_module.plan_archive(result, archive_root)

    assert plan["schema_version"] == "1"
    assert plan["source"] == str(result.resolve())
    assert plan["destination"] == str((archive_root / result.name).resolve())
    assert not archive_root.exists()

    included = {entry["path"]: entry for entry in plan["included"]}
    expected_included = {
        "metrics.json",
        "summary-final.json",
        "benchmark_summary.json",
        "retrieval_metrics.json",
        "answer_aux_metrics.json",
        "diagnostics.json",
        "latency-p95.json",
        "content-overlap.json",
        "resolved_config.yaml",
        "config/resolved-config.json",
        "resolved-system-config.json",
        "report.md",
        "report-details.txt",
        "run.log",
        "answer-01.json",
        "nested/search-trace.json",
        "nested/eval-results.json",
        "logs/server.log",
        "logs/context.txt",
        "artifacts/evidence.csv",
        "artifacts/nested/note.txt",
    }
    assert set(included) == expected_included
    assert all(entry["size"] > 0 and entry["reason"] for entry in included.values())
    assert included["resolved-system-config.json"]["reason"] == "resolved_config"

    excluded = {entry["path"]: entry["reason"] for entry in plan["excluded"]}
    assert excluded["random.bin"] == "not_compact_evidence"
    assert excluded["nested/ordinary.json"] == "not_compact_evidence"
    assert excluded["artifacts/blob.bin"] == "not_compact_evidence"
    assert excluded["compiled.pyc"] == "compiled_python"
    assert excluded["linked.json"] == "symlink_not_followed"
    assert excluded["artifacts"] == "traversed_directory"
    for component in [
        "openclaw-workspaces",
        "node_modules",
        ".cache",
        ".venv",
        "__pycache__",
        "vectors",
        "vector_index",
        "bm25-index",
        "model_cache",
    ]:
        assert excluded[component].startswith("pruned_component:")
        assert f"{component}/must-not-be-visited.json" not in included
        assert f"{component}/must-not-be-visited.json" not in excluded


def test_plan_archive_rejects_non_directory_and_symlink_sources(tmp_path: Path) -> None:
    archive_module = _archive_module()
    assert hasattr(archive_module, "plan_archive")
    source_file = _write(tmp_path / "result.txt", "not a result directory\n")
    source_directory = tmp_path / "real-result"
    source_directory.mkdir()
    source_symlink = tmp_path / "linked-result"
    source_symlink.symlink_to(source_directory, target_is_directory=True)

    with pytest.raises(ValueError, match="regular directory"):
        archive_module.plan_archive(source_file, tmp_path / "archives")
    with pytest.raises(ValueError, match="regular directory"):
        archive_module.plan_archive(source_symlink, tmp_path / "archives")


def test_plan_archive_rejects_destination_inside_source_or_collision(
    tmp_path: Path,
) -> None:
    archive_module = _archive_module()
    assert hasattr(archive_module, "plan_archive")
    result = tmp_path / "result"
    result.mkdir()

    with pytest.raises(ValueError, match="inside the source"):
        archive_module.plan_archive(result, result / "archives")
    with pytest.raises(FileExistsError, match="destination already exists"):
        archive_module.plan_archive(result, result.parent)


def test_create_archive_dry_run_returns_plan_without_creating_archive_root(
    tmp_path: Path,
) -> None:
    archive_module = _archive_module()
    assert hasattr(archive_module, "create_archive")
    result = _make_archive_plan_fixture(tmp_path)
    archive_root = tmp_path / "dry-run-archives"

    plan = archive_module.create_archive(result, archive_root, execute=False)

    assert plan["destination"] == str((archive_root / result.name).resolve())
    assert plan["included"]
    assert len(plan["plan_digest"]) == 64
    assert not archive_root.exists()


def test_create_archive_expected_digest_rejects_a_changed_plan_without_writes(
    tmp_path: Path,
) -> None:
    archive_module = _archive_module()
    result = tmp_path / "result"
    _write(result / "metrics.json", "{}\n")
    archive_root = tmp_path / "archives"
    reviewed_plan = archive_module.create_archive(result, archive_root, execute=False)
    _write(result / "answer-secret.json", "{}\n")

    with pytest.raises(ValueError, match="plan changed"):
        archive_module.create_archive(
            result,
            archive_root,
            execute=True,
            expected_plan_digest=reviewed_plan["plan_digest"],
        )

    assert not archive_root.exists()


def test_create_archive_rejects_size_change_after_plan_without_final_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_module = _archive_module()
    result = tmp_path / "result"
    metrics = _write(result / "metrics.json", "{}\n")
    archive_root = tmp_path / "archives"
    reviewed_plan = archive_module.create_archive(result, archive_root, execute=False)

    def mutate_after_plan(_source: Path) -> tuple[str, None]:
        metrics.write_text('{"changed": true}\n', encoding="utf-8")
        return "test-sha", None

    monkeypatch.setattr(archive_module, "_git_sha", mutate_after_plan)

    with pytest.raises((OSError, ValueError), match="size|changed"):
        archive_module.create_archive(
            result,
            archive_root,
            execute=True,
            expected_plan_digest=reviewed_plan["plan_digest"],
        )

    assert not (archive_root / result.name).exists()


def test_create_archive_rejects_source_growth_during_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_module = _archive_module()
    result = tmp_path / "result"
    metrics = _write(result / "metrics.json", "planned bytes\n")
    archive_root = tmp_path / "archives"
    source_inode = metrics.stat().st_ino
    original_read = os.read
    changed = False

    def mutate_after_first_source_read(descriptor: int, size: int) -> bytes:
        nonlocal changed
        chunk = original_read(descriptor, size)
        if not changed and chunk and os.fstat(descriptor).st_ino == source_inode:
            with metrics.open("ab") as stream:
                stream.write(b"changed during copy\n")
            changed = True
        return chunk

    monkeypatch.setattr(archive_module.os, "read", mutate_after_first_source_read)

    with pytest.raises((OSError, ValueError), match="changed|size"):
        archive_module.create_archive(result, archive_root, execute=True)

    assert changed is True
    assert not (archive_root / result.name).exists()


def test_create_archive_rejects_symlinked_source_ancestor_after_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_module = _archive_module()
    result = tmp_path / "result"
    source_directory = result / "artifacts"
    _write(source_directory / "evidence.csv", "trusted\n")
    outside_directory = tmp_path / "outside"
    _write(outside_directory / "evidence.csv", "outside\n")
    archive_root = tmp_path / "archives"
    reviewed_plan = archive_module.create_archive(result, archive_root, execute=False)

    def swap_ancestor_after_plan(_source: Path) -> tuple[str, None]:
        source_directory.rename(result / "artifacts-original")
        source_directory.symlink_to(outside_directory, target_is_directory=True)
        return "test-sha", None

    monkeypatch.setattr(archive_module, "_git_sha", swap_ancestor_after_plan)

    with pytest.raises((OSError, ValueError), match="symlink|directory|regular"):
        archive_module.create_archive(
            result,
            archive_root,
            execute=True,
            expected_plan_digest=reviewed_plan["plan_digest"],
        )

    assert not (archive_root / result.name).exists()


def test_create_archive_opens_replaced_fifo_nonblocking_and_rejects_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_module = _archive_module()
    result = tmp_path / "result"
    metrics = _write(result / "metrics.json", "{}\n")
    archive_root = tmp_path / "archives"
    reviewed_plan = archive_module.create_archive(result, archive_root, execute=False)

    def replace_with_fifo_after_plan(_source: Path) -> tuple[str, None]:
        metrics.unlink()
        os.mkfifo(metrics)
        return "test-sha", None

    monkeypatch.setattr(archive_module, "_git_sha", replace_with_fifo_after_plan)
    original_os_open = os.open

    def require_nonblocking_fifo(
        path: object, flags: int, *args: object, **kwargs: object
    ):
        if os.fspath(path).endswith("metrics.json") and not flags & os.O_NONBLOCK:
            raise AssertionError("FIFO source was opened without O_NONBLOCK")
        return original_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(archive_module.os, "open", require_nonblocking_fifo)

    with pytest.raises((OSError, ValueError), match="regular"):
        archive_module.create_archive(
            result,
            archive_root,
            execute=True,
            expected_plan_digest=reviewed_plan["plan_digest"],
        )

    assert not (archive_root / result.name).exists()


def test_create_archive_does_not_follow_swapped_archive_root_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_module = _archive_module()
    result = tmp_path / "result"
    _write(result / "metrics.json", "{}\n")
    safe_parent = tmp_path / "safe"
    safe_parent.mkdir()
    archive_root = safe_parent / "archives"
    outside = tmp_path / "outside"
    outside.mkdir()
    reviewed_plan = archive_module.create_archive(result, archive_root, execute=False)

    def swap_destination_ancestor_after_plan(_source: Path) -> tuple[str, None]:
        safe_parent.rename(tmp_path / "safe-original")
        safe_parent.symlink_to(outside, target_is_directory=True)
        return "test-sha", None

    monkeypatch.setattr(
        archive_module, "_git_sha", swap_destination_ancestor_after_plan
    )

    with pytest.raises((OSError, ValueError), match="symlink|directory"):
        archive_module.create_archive(
            result,
            archive_root,
            execute=True,
            expected_plan_digest=reviewed_plan["plan_digest"],
        )

    assert not (outside / "archives").exists()
    assert not (tmp_path / "safe-original" / "archives" / result.name).exists()


def test_create_archive_revalidates_held_archive_root_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_module = _archive_module()
    result = tmp_path / "result"
    _write(result / "metrics.json", "{}\n")
    archive_root = tmp_path / "archives"
    archive_root.mkdir()
    moved_archive_root = tmp_path / "archives-moved"
    outside = tmp_path / "outside"
    outside.mkdir()
    original_copy = archive_module._copy_archive_entry
    swapped = False

    def swap_root_then_copy(*args: object, **kwargs: object):
        nonlocal swapped
        if not swapped:
            archive_root.rename(moved_archive_root)
            archive_root.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_copy(*args, **kwargs)

    monkeypatch.setattr(archive_module, "_copy_archive_entry", swap_root_then_copy)

    with pytest.raises((OSError, ValueError), match="archive root.*changed|symlink"):
        archive_module.create_archive(result, archive_root, execute=True)

    assert swapped is True
    assert not (outside / result.name).exists()
    assert not (moved_archive_root / result.name).exists()
    assert list(moved_archive_root.iterdir()) == []


def test_create_archive_revalidates_held_source_root_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_module = _archive_module()
    result = tmp_path / "result"
    _write(result / "metrics.json", "{}\n")
    moved_result = tmp_path / "result-moved"
    outside = tmp_path / "outside"
    outside.mkdir()
    archive_root = tmp_path / "archives"
    original_copy = archive_module._copy_archive_entry
    swapped = False

    def copy_then_swap_source(*args: object, **kwargs: object):
        nonlocal swapped
        copied = original_copy(*args, **kwargs)
        if not swapped:
            result.rename(moved_result)
            result.symlink_to(outside, target_is_directory=True)
            swapped = True
        return copied

    monkeypatch.setattr(archive_module, "_copy_archive_entry", copy_then_swap_source)

    with pytest.raises((OSError, ValueError), match="archive source.*changed|symlink"):
        archive_module.create_archive(result, archive_root, execute=True)

    assert swapped is True
    assert not (archive_root / result.name).exists()
    assert list(archive_root.iterdir()) == []


@pytest.mark.parametrize("tamper_kind", ["symlink", "same_size_content"])
def test_create_archive_revalidates_staging_content_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper_kind: str
) -> None:
    archive_module = _archive_module()
    result = tmp_path / "result"
    _write(result / "metrics.json", "{}\n")
    outside = _write(tmp_path / "outside", "outside\n")
    archive_root = tmp_path / "archives"
    original_write_manifest = archive_module._write_manifest_at

    def write_manifest_then_tamper(
        staging_fd: int, manifest: dict[str, object]
    ) -> None:
        original_write_manifest(staging_fd, manifest)
        os.unlink("metrics.json", dir_fd=staging_fd)
        if tamper_kind == "symlink":
            os.symlink(outside, "metrics.json", dir_fd=staging_fd)
        else:
            descriptor = os.open(
                "metrics.json",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=staging_fd,
            )
            try:
                os.write(descriptor, b"xx\n")
            finally:
                os.close(descriptor)

    monkeypatch.setattr(
        archive_module, "_write_manifest_at", write_manifest_then_tamper
    )

    with pytest.raises((OSError, ValueError), match="staging|regular|checksum"):
        archive_module.create_archive(result, archive_root, execute=True)

    assert not (archive_root / result.name).exists()
    assert list(archive_root.iterdir()) == []
    assert outside.read_text(encoding="utf-8") == "outside\n"


@pytest.mark.parametrize("extra_kind", ["regular", "symlink"])
def test_create_archive_rejects_unmanifested_staging_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra_kind: str
) -> None:
    archive_module = _archive_module()
    result = tmp_path / "result"
    _write(result / "metrics.json", "{}\n")
    outside = _write(tmp_path / "outside", "outside\n")
    archive_root = tmp_path / "archives"
    original_write_manifest = archive_module._write_manifest_at

    def write_manifest_then_inject(
        staging_fd: int, manifest: dict[str, object]
    ) -> None:
        original_write_manifest(staging_fd, manifest)
        if extra_kind == "symlink":
            os.symlink(outside, "unmanifested", dir_fd=staging_fd)
        else:
            descriptor = os.open(
                "unmanifested",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=staging_fd,
            )
            os.close(descriptor)

    monkeypatch.setattr(
        archive_module, "_write_manifest_at", write_manifest_then_inject
    )

    with pytest.raises((OSError, ValueError), match="unmanifested|unexpected"):
        archive_module.create_archive(result, archive_root, execute=True)

    assert not (archive_root / result.name).exists()
    assert list(archive_root.iterdir()) == []
    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_create_archive_publish_collision_preserves_existing_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_module = _archive_module()
    result = tmp_path / "result"
    _write(result / "metrics.json", "{}\n")
    archive_root = tmp_path / "archives"
    original_write_manifest = getattr(archive_module, "_write_manifest_at", None)
    assert callable(original_write_manifest), "missing manifest publication boundary"
    collision_inode: int | None = None

    def create_collision_then_write(*args: object, **kwargs: object):
        nonlocal collision_inode
        collision = archive_root / result.name
        collision.mkdir()
        collision_inode = collision.stat().st_ino
        return original_write_manifest(*args, **kwargs)

    monkeypatch.setattr(
        archive_module, "_write_manifest_at", create_collision_then_write
    )

    with pytest.raises(FileExistsError, match="destination already exists"):
        archive_module.create_archive(result, archive_root, execute=True)

    assert collision_inode is not None
    assert (archive_root / result.name).stat().st_ino == collision_inode
    assert list((archive_root / result.name).iterdir()) == []
    assert sorted(path.name for path in archive_root.iterdir()) == [result.name]


def test_create_archive_reports_post_publish_fsync_failure_without_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_module = _archive_module()
    result = tmp_path / "result"
    _write(result / "metrics.json", "{}\n")
    archive_root = tmp_path / "archives"
    original_rename = archive_module._rename_no_replace
    original_fsync = os.fsync
    renamed = False

    def rename_then_mark(*args: object, **kwargs: object) -> None:
        nonlocal renamed
        original_rename(*args, **kwargs)
        renamed = True

    def fail_fsync_after_rename(descriptor: int) -> None:
        if renamed:
            raise OSError("injected archive-root fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(archive_module, "_rename_no_replace", rename_then_mark)
    monkeypatch.setattr(archive_module.os, "fsync", fail_fsync_after_rename)

    with pytest.raises(OSError, match="published.*fsync"):
        archive_module.create_archive(result, archive_root, execute=True)

    destination = archive_root / result.name
    assert (destination / "manifest.json").is_file()
    assert sorted(path.name for path in archive_root.iterdir()) == [result.name]


def test_create_archive_closes_source_fd_when_staging_parent_open_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_module = _archive_module()
    result = tmp_path / "result"
    _write(result / "artifacts" / "evidence.csv", "evidence\n")
    archive_root = tmp_path / "archives"
    original_open_relative = archive_module._open_relative_directory_at
    before = len(list(Path("/proc/self/fd").iterdir()))

    def fail_staging_parent(
        root_fd: int, components: tuple[str, ...], *, create: bool, purpose: str
    ) -> int:
        if create and "staging ancestor" in purpose:
            raise OSError("injected staging parent failure")
        return original_open_relative(
            root_fd, components, create=create, purpose=purpose
        )

    monkeypatch.setattr(
        archive_module, "_open_relative_directory_at", fail_staging_parent
    )

    with pytest.raises(OSError, match="staging parent"):
        archive_module.create_archive(result, archive_root, execute=True)

    after = len(list(Path("/proc/self/fd").iterdir()))
    assert after == before
    assert list(archive_root.iterdir()) == []


def test_create_archive_cleans_new_staging_when_open_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_module = _archive_module()
    result = tmp_path / "result"
    _write(result / "metrics.json", "{}\n")
    archive_root = tmp_path / "archives"
    original_open = os.open

    def fail_staging_open(path: object, flags: int, *args: object, **kwargs: object):
        if os.fspath(path).startswith(".artifact-hygiene-staging-"):
            raise OSError("injected staging open failure")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(archive_module.os, "open", fail_staging_open)

    with pytest.raises(OSError, match="staging open"):
        archive_module.create_archive(result, archive_root, execute=True)

    assert list(archive_root.iterdir()) == []


@pytest.mark.parametrize(
    "failure_stage", ["created_stat", "opened_fstat", "current_stat", "root_fsync"]
)
def test_create_archive_rolls_back_staging_initialization_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_stage: str
) -> None:
    archive_module = _archive_module()
    result = tmp_path / "result"
    _write(result / "metrics.json", "{}\n")
    archive_root = tmp_path / "archives"
    original_stat = os.stat
    original_fstat = os.fstat
    original_fsync = os.fsync
    staging_stat_calls = 0
    injected = False

    def is_staging_descriptor(descriptor: int) -> bool:
        try:
            target = os.readlink(f"/proc/self/fd/{descriptor}")
        except OSError:
            return False
        return Path(target).name.startswith(".artifact-hygiene-staging-")

    def fail_selected_stat(path: object, *args: object, **kwargs: object):
        nonlocal staging_stat_calls, injected
        if os.fspath(path).startswith(".artifact-hygiene-staging-"):
            staging_stat_calls += 1
            selected_call = 1 if failure_stage == "created_stat" else 2
            if failure_stage in {"created_stat", "current_stat"} and (
                staging_stat_calls == selected_call and not injected
            ):
                injected = True
                raise OSError(f"injected {failure_stage} failure")
        return original_stat(path, *args, **kwargs)

    def fail_selected_fstat(descriptor: int):
        nonlocal injected
        if (
            failure_stage == "opened_fstat"
            and not injected
            and is_staging_descriptor(descriptor)
        ):
            injected = True
            raise OSError("injected opened_fstat failure")
        return original_fstat(descriptor)

    def fail_selected_fsync(descriptor: int) -> None:
        nonlocal injected
        try:
            target = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        except OSError:
            target = Path()
        if failure_stage == "root_fsync" and not injected and target == archive_root:
            injected = True
            raise OSError("injected root_fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(archive_module.os, "stat", fail_selected_stat)
    monkeypatch.setattr(archive_module.os, "fstat", fail_selected_fstat)
    monkeypatch.setattr(archive_module.os, "fsync", fail_selected_fsync)

    with pytest.raises(OSError, match=failure_stage):
        archive_module.create_archive(result, archive_root, execute=True)

    assert injected is True
    assert list(archive_root.iterdir()) == []


@pytest.mark.parametrize("failure_hook", ["_copy_archive_entry", "_write_manifest_at"])
def test_create_archive_failure_cleans_staging_and_allows_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_hook: str
) -> None:
    archive_module = _archive_module()
    result = tmp_path / "result"
    _write(result / "metrics.json", "{}\n")
    archive_root = tmp_path / "archives"
    original_hook = getattr(archive_module, failure_hook, None)
    assert callable(original_hook), f"missing failure boundary: {failure_hook}"

    class InjectedFailure(BaseException):
        pass

    def fail_once(*_args: object, **_kwargs: object):
        raise InjectedFailure(failure_hook)

    monkeypatch.setattr(archive_module, failure_hook, fail_once)

    with pytest.raises(InjectedFailure):
        archive_module.create_archive(result, archive_root, execute=True)

    assert not (archive_root / result.name).exists()
    if archive_root.exists():
        assert list(archive_root.iterdir()) == []

    monkeypatch.setattr(archive_module, failure_hook, original_hook)
    manifest = archive_module.create_archive(result, archive_root, execute=True)
    assert manifest["included"][0]["path"] == "metrics.json"
    assert (archive_root / result.name / "manifest.json").is_file()


def test_create_archive_copies_only_included_regular_files_and_verifies_hashes(
    tmp_path: Path,
) -> None:
    archive_module = _archive_module()
    assert hasattr(archive_module, "create_archive")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Artifact Test"], check=True
    )

    result = repo / "evaluation" / "results" / "result-run"
    metrics = _write(result / "metrics.json", '{"score": 0.9}\n')
    report = _write(result / "report.md", "# Result\n")
    evidence = _write(result / "artifacts" / "evidence.csv", "metric,value\n")
    excluded = _write(result / "runtime-state.bin", "do not archive\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "fixture"], check=True
    )
    git_sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    source_snapshot = {
        path.relative_to(result).as_posix(): path.read_bytes()
        for path in (metrics, report, evidence, excluded)
    }
    archive_root = repo / "evaluation" / "archives"

    manifest = archive_module.create_archive(result, archive_root, execute=True)

    destination = archive_root / result.name
    manifest_path = destination / "manifest.json"
    assert manifest == json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "1"
    assert manifest["source"] == str(result.resolve())
    assert manifest["destination"] == str(destination.resolve())
    assert manifest["git_sha"] == git_sha
    assert manifest["git_sha_error"] is None
    assert manifest["archived_at"].endswith("Z")

    archived_entries = {entry["path"]: entry for entry in manifest["included"]}
    assert set(archived_entries) == {
        "artifacts/evidence.csv",
        "metrics.json",
        "report.md",
    }
    for relative_path, entry in archived_entries.items():
        source_bytes = source_snapshot[relative_path]
        destination_file = destination / relative_path
        assert destination_file.read_bytes() == source_bytes
        assert entry["size"] == len(source_bytes)
        assert entry["sha256"] == hashlib.sha256(source_bytes).hexdigest()
        assert not Path(relative_path).is_absolute()
        assert ".." not in PurePosixPath(relative_path).parts
    assert not (destination / "runtime-state.bin").exists()
    assert {
        path.relative_to(result).as_posix(): path.read_bytes()
        for path in (metrics, report, evidence, excluded)
    } == source_snapshot

    with pytest.raises(FileExistsError, match="destination already exists"):
        archive_module.create_archive(result, archive_root, execute=True)


def test_create_archive_records_unknown_git_sha_outside_repository(
    tmp_path: Path,
) -> None:
    archive_module = _archive_module()
    assert hasattr(archive_module, "create_archive")
    result = tmp_path / "result"
    _write(result / "summary.json", "{}\n")

    manifest = archive_module.create_archive(
        result, tmp_path / "archives", execute=True
    )

    assert manifest["git_sha"] == "unknown"
    assert manifest["git_sha_error"]


def test_archive_cli_dry_run_prints_plan_and_execute_flag_is_required_to_write(
    tmp_path: Path,
) -> None:
    result = tmp_path / "result"
    _write(result / "metrics.json", "{}\n")
    archive_root = tmp_path / "archives"
    base_command = [
        sys.executable,
        "-m",
        "evaluation.tools.artifact_hygiene",
        "archive",
        "--result",
        str(result),
        "--archive-root",
        str(archive_root),
    ]

    dry_run = subprocess.run(base_command, check=False, capture_output=True, text=True)

    assert dry_run.returncode == 0, dry_run.stderr
    dry_plan = json.loads(dry_run.stdout)
    assert dry_plan["destination"] == str((archive_root / result.name).resolve())
    assert not archive_root.exists()

    executed = subprocess.run(
        [*base_command, "--execute", "--expected-plan-digest", dry_plan["plan_digest"]],
        check=False,
        capture_output=True,
        text=True,
    )

    assert executed.returncode == 0, executed.stderr
    manifest = json.loads(executed.stdout)
    assert manifest["included"][0]["path"] == "metrics.json"
    assert (archive_root / result.name / "manifest.json").is_file()


def test_cli_help_has_inventory_and_archive_but_no_delete_subcommand() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "evaluation.tools.artifact_hygiene", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "inventory" in completed.stdout
    assert "archive" in completed.stdout
    assert "delete" not in completed.stdout.casefold()
