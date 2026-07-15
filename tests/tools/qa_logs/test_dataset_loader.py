import json, pytest
from pathlib import Path
from evaluation.tools.qa_logs.dataset_loader import load_qa, QAFields

@pytest.fixture
def fixture_dataset(tmp_path):
    data = [{
        "sample_id": "locomo_7",
        "qa": [
            {"question": "When did Deborah's father pass away?",
             "answer": "2023-01-25", "category": 3,
             "evidence": ["D2:1"]}
        ],
        "conversation": {"session_2_date_time": "2023-01-27 (Friday)",
                         "session_2": [
                             {"speaker": "Deborah", "text": "My dad passed away two days ago...", "dia_id": "D2:1"}
                         ]},
    }]
    p = tmp_path / "ds.json"
    p.write_text(json.dumps(data))
    return p

def test_load_qa_by_qid(fixture_dataset):
    qa = load_qa(fixture_dataset, qid="locomo_7_qa0")
    assert qa.conv == "locomo_7"
    assert qa.question == "When did Deborah's father pass away?"
    assert qa.golden == "2023-01-25"
    assert qa.category == 3
    assert qa.evidence_turns[0].speaker == "Deborah"
    assert "passed away" in qa.evidence_turns[0].text
