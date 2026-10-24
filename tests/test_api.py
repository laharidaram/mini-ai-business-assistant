"""HTTP-level tests: status codes and response shape."""
import pytest
from fastapi.testclient import TestClient
from app.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return TestClient(create_app(db_path=tmp_path / "api.db"))


def test_ask_returns_structured_answer(client):
    r = client.post("/ask", json={"question": "What is the total revenue?"})
    body = r.json()
    assert r.status_code == 200 and body["status"] == "ok"
    assert "14,39,200" in body["answer"] and body["rows"] and body["sql"] and body["interpreted_by"] == "rules"
    assert "49 of 51" in body["notes"][-1]


def test_empty_question_is_422(client):
    r = client.post("/ask", json={"question": "   "})
    assert r.status_code == 422 and r.json()["status"] == "error"


def test_malformed_body_is_422(client):
    assert client.post("/ask", json={"q": "hi"}).status_code == 422
    assert client.post("/ask", json={"question": 123}).status_code == 422


def test_unsupported_question_is_400_with_suggestions(client):
    r = client.post("/ask", json={"question": "Tell me a joke"})
    assert r.status_code == 400 and len(r.json()["supported_questions"]) >= 5 and "can answer" in r.json()["message"]


def test_ranking_and_missing_price_questions(client):
    low = client.post("/ask", json={"question": "Which product generated the lowest revenue?"}).json()
    assert low["answer"].startswith("Laptop Stand generated the lowest")
    gap = client.post("/ask", json={"question": "Which transactions have a missing unit price?"}).json()
    assert gap["intent"] == "missing_price" and "TXN0051" in gap["answer"] and "TXN0034" not in gap["answer"]


def test_data_quality_endpoint(client):
    d = client.get("/api/data-quality").json()
    assert d["loaded"] == 51 and d["rejected"] == [] and d["by_status"] == {"Completed": 50, "Returned": 1}
    assert d["revenue_status"] == {"valid": 49, "zero_quantity": 1, "unknown_price": 1}
    assert [f["transaction_id"] for f in d["flagged"]] == ["TXN0034", "TXN0051"]


def test_mode_endpoint_reports_rules_without_key(client):
    assert client.get("/api/mode").json() == {"mode": "rules", "model": None}


def test_mode_endpoint_reports_llm_with_key(client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    assert client.get("/api/mode").json()["mode"] == "llm"


def test_ui_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "Sales Q&amp;A" in r.text
