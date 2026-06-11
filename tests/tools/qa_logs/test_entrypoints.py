from evaluation.tools.qa_logs.__main__ import main as package_main
from evaluation.tools.qa_logs.dump import main as dump_main


def test_package_entrypoint_lists_explicit_tools(capsys):
    code = package_main()

    captured = capsys.readouterr()
    assert code == 2
    assert "evaluation.tools.qa_logs.dump" in captured.err
    assert "evaluation.tools.qa_logs.materialize_qa_jsonl" in captured.err


def test_dump_entrypoint_keeps_old_cli_behavior(monkeypatch, tmp_path, capsys):
    calls = []

    def fake_dump(args, out_dir):
        calls.append((args, out_dir))

    monkeypatch.setattr("evaluation.tools.qa_logs.dump.dump_qa_logs", fake_dump)
    code = dump_main(
        [
            "--qid",
            "locomo_7_qa_9",
            "--run-name",
            "testrun",
            "--results-root",
            str(tmp_path),
            "--ovdata",
            str(tmp_path),
            "--dataset",
            str(tmp_path / "dataset.json"),
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert calls[0][0].qid == "locomo_7_qa9"
    assert calls[0][1].as_posix().endswith("reports/qa_logs/testrun/locomo_7_qa9")
    assert "wrote raw dump" in captured.out
