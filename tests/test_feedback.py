import json

import app as app_module

ADMIN = ("admin", "adminpw")


def join(client, name="Mira"):
    res = client.post("/api/join", json={"name": name})
    assert res.status_code == 200
    return res.json()["token"]


def test_feedback_requires_token(client):
    assert client.post("/api/feedback", json={"text": "hi"}).status_code == 401


def test_feedback_stored_anonymously(client):
    token = join(client)
    res = client.post(
        "/api/feedback",
        json={"text": "  more   breaks  "},
        headers={"X-Token": token},
    )
    assert res.status_code == 200
    feedback = client.get("/api/admin/state", auth=ADMIN).json()["feedback"]
    assert len(feedback) == 1
    assert feedback[0]["text"] == "more breaks"  # whitespace collapsed
    assert set(feedback[0]) == {"id", "text", "ts"}  # no name field


def test_feedback_validation(client):
    headers = {"X-Token": join(client)}
    assert client.post("/api/feedback", json={"text": "   "}, headers=headers).status_code == 422
    assert client.post("/api/feedback", json={"text": "x" * 501}, headers=headers).status_code == 422


def test_feedback_cap(client, monkeypatch):
    monkeypatch.setattr(app_module, "MAX_FEEDBACK_PER_PERSON", 2)
    headers = {"X-Token": join(client)}
    for i in range(2):
        assert client.post("/api/feedback", json={"text": f"fb {i}"}, headers=headers).status_code == 200
    assert client.post("/api/feedback", json={"text": "fb 3"}, headers=headers).status_code == 429


def test_feedback_newest_first(client):
    headers = {"X-Token": join(client)}
    client.post("/api/feedback", json={"text": "first"}, headers=headers)
    client.post("/api/feedback", json={"text": "second"}, headers=headers)
    feedback = client.get("/api/admin/state", auth=ADMIN).json()["feedback"]
    assert [f["text"] for f in feedback] == ["second", "first"]


def test_reset_clears_feedback(client):
    headers = {"X-Token": join(client)}
    client.post("/api/feedback", json={"text": "bye"}, headers=headers)
    res = client.post("/api/admin/reset", auth=ADMIN, headers={"X-Admin-Action": "1"})
    assert res.status_code == 200
    assert client.get("/api/admin/state", auth=ADMIN).json()["feedback"] == []


def test_state_file_without_feedback_key_migrates(client):
    # Simulate a data file written before this feature existed.
    app_module.DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    app_module.DATA_FILE.write_text(json.dumps({"participants": {}, "questions": []}))
    headers = {"X-Token": join(client)}
    res = client.post("/api/feedback", json={"text": "works"}, headers=headers)
    assert res.status_code == 200
