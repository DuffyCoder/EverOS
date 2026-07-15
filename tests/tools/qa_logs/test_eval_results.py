import json
from evaluation.tools.qa_logs.eval_results import load_eval_result, list_wrong_qids


def test_load_eval_result_legacy_flat_results(tmp_path):
    p = tmp_path / "eval_results.json"
    p.write_text(json.dumps({"results": [
        {"question_id": "locomo_7_qa9", "generated_answer": "No info",
         "judge": {"correct": False}, "latency_ms": 9411}
    ]}))
    r = load_eval_result(p, "locomo_7_qa9")
    assert r.generated == "No info"
    assert r.correct is False
    assert r.latency_ms == 9411


def test_list_wrong_qids_legacy(tmp_path):
    p = tmp_path / "eval_results.json"
    p.write_text(json.dumps({"results": [
        {"question_id": "q1", "judge": {"correct": True}},
        {"question_id": "q2", "judge": {"correct": False}},
        {"question_id": "q3", "judge": {"correct": False}},
    ]}))
    assert list_wrong_qids(p) == ["q2", "q3"]


def test_load_eval_result_real_schema(tmp_path):
    """Real schema: detailed_results is a dict keyed by user, each value a list
    of records with `generated_answer` and top-level `is_correct`."""
    p = tmp_path / "eval_results.json"
    p.write_text(json.dumps({
        "total_questions": 3,
        "correct": 1,
        "accuracy": 0.33,
        "detailed_results": {
            "locomo_exp_user_0": [
                {"question_id": "locomo_0_qa0", "generated_answer": "right",
                 "golden_answer": "right", "is_correct": True,
                 "llm_judgments": {"judgment_1": True, "judgment_2": True, "judgment_3": True}},
            ],
            "locomo_exp_user_7": [
                {"question_id": "locomo_7_qa9", "generated_answer": "no info",
                 "golden_answer": "January 25, 2023", "is_correct": False,
                 "llm_judgments": {"judgment_1": False, "judgment_2": False, "judgment_3": False}},
            ],
        },
    }))
    r = load_eval_result(p, "locomo_7_qa9")
    assert r.generated == "no info"
    assert r.correct is False
    assert r.judgments == {"judgment_1": False, "judgment_2": False, "judgment_3": False}


def test_list_wrong_qids_real_schema(tmp_path):
    p = tmp_path / "eval_results.json"
    p.write_text(json.dumps({
        "detailed_results": {
            "u0": [
                {"question_id": "q1", "is_correct": True},
                {"question_id": "q2", "is_correct": False},
            ],
            "u1": [
                {"question_id": "q3", "is_correct": False},
                {"question_id": "q4", "is_correct": True},
            ],
        },
    }))
    assert sorted(list_wrong_qids(p)) == ["q2", "q3"]
