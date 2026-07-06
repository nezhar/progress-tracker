import copy
import json

import pytest

import app as app_module

ADMIN = ("admin", "adminpw")
ACTION = {"X-Admin-Action": "1"}


def write_state(data):
    app_module.DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    app_module.DATA_FILE.write_text(json.dumps(data))


def only_session(state):
    assert len(state["sessions"]) == 1
    return next(iter(state["sessions"].values()))


def test_legacy_state_migrates_to_inactive_session(client):
    write_state(
        {
            "participants": {
                "Mira": {
                    "token": "tok-old",
                    "checks": {"Setup": True},
                    "joined_at": 10.0,
                }
            },
            "questions": [
                {
                    "id": "q1",
                    "name": "Mira",
                    "text": "Can you repeat that?",
                    "ts": 11.0,
                    "answered": True,
                    "votes": {"Mira": 1},
                    "reply": "Yes",
                }
            ],
            "feedback": [{"id": "f1", "text": "More time", "ts": 12.0}],
        }
    )

    res = client.get("/api/admin/sessions", auth=ADMIN)

    assert res.status_code == 200
    sessions = res.json()["sessions"]
    assert len(sessions) == 1
    migrated = sessions[0]
    assert migrated["name"] == "Imported workshop"
    assert migrated["active"] is False
    assert migrated["participant_count"] == 1
    assert migrated["question_count"] == 1
    assert migrated["feedback_count"] == 1
    assert res.json()["active_session_id"] is None


def test_legacy_migration_preserves_participant_details_and_adds_defaults(client):
    legacy_exercise = "Legacy-only Exercise"
    write_state(
        {
            "participants": {
                "Mira": {
                    "token": "tok-old",
                    "checks": {"Setup": True, legacy_exercise: True},
                    "joined_at": 10.0,
                }
            },
            "questions": [],
        }
    )

    state = app_module.load_state()

    row = only_session(state)["participants"]["Mira"]
    assert row["token"] == "tok-old"
    assert row["checks"]["Setup"] is True
    assert row["checks"][legacy_exercise] is True
    for exercise in app_module.EXERCISES:
        assert exercise in row["checks"]
        if exercise not in {"Setup", legacy_exercise}:
            assert row["checks"][exercise] is False


def test_legacy_migration_defaults_missing_feedback_and_question_fields(client):
    write_state(
        {
            "participants": {},
            "questions": [
                {
                    "id": "q1",
                    "name": "Mira",
                    "text": "Can you repeat that?",
                    "ts": 11.0,
                }
            ],
        }
    )

    state = app_module.load_state()

    session = only_session(state)
    assert session["feedback"] == []
    assert session["questions"][0]["votes"] == {}
    assert session["questions"][0]["reply"] == ""
    assert session["questions"][0]["answered"] is False


def test_invalid_active_session_id_normalizes_to_none(client):
    write_state(
        {
            "active_session_id": "missing",
            "sessions": {
                "s1": {
                    "id": "s1",
                    "name": "Morning",
                    "access_code": "ROOM1",
                    "created_at": 10.0,
                    "participants": {},
                    "questions": [],
                    "feedback": [],
                }
            },
        }
    )

    res = client.get("/api/admin/sessions", auth=ADMIN)

    assert res.status_code == 200
    assert res.json()["active_session_id"] is None
    assert res.json()["sessions"][0]["active"] is False


def test_legacy_migration_session_id_is_stable_across_loads(client):
    write_state(
        {
            "participants": {
                "Mira": {
                    "token": "tok-old",
                    "checks": {"Setup": True},
                    "joined_at": 10.0,
                }
            },
            "questions": [],
            "feedback": [],
        }
    )

    first = client.get("/api/admin/sessions", auth=ADMIN)
    second = client.get("/api/admin/sessions", auth=ADMIN)

    assert first.status_code == 200
    assert second.status_code == 200
    first_id = first.json()["sessions"][0]["id"]
    second_id = second.json()["sessions"][0]["id"]
    assert second_id == first_id
    persisted = json.loads(app_module.DATA_FILE.read_text())
    assert list(persisted["sessions"]) == [first_id]


def test_no_active_admin_state_returns_empty_board(client):
    res = client.get("/api/admin/state", auth=ADMIN)

    assert res.status_code == 200
    assert res.json() == {
        "exercises": app_module.EXERCISES,
        "rows": [],
        "counts": {exercise: 0 for exercise in app_module.EXERCISES},
        "questions": [],
        "feedback": [],
    }


def test_no_active_participant_endpoints_reject_token(client):
    write_state(
        {
            "participants": {
                "Mira": {
                    "token": "tok-old",
                    "checks": {"Setup": True},
                    "joined_at": 10.0,
                }
            },
            "questions": [],
            "feedback": [],
        }
    )

    res = client.get("/api/me", headers={"X-Token": "tok-old"})

    assert res.status_code == 401
    assert res.json()["detail"] == "Unknown or missing token"


def test_no_active_admin_question_actions_return_no_active_session(client):
    res = client.post(
        "/api/admin/answer",
        auth=ADMIN,
        headers={"X-Admin-Action": "1"},
        json={"id": "q1", "answered": True},
    )

    assert res.status_code == 404
    assert res.json()["detail"] == "No active session"


def test_legacy_admin_reset_is_gone(client):
    res = client.post(
        "/api/admin/reset",
        auth=ADMIN,
        headers={"X-Admin-Action": "1"},
    )

    assert res.status_code == 410
    assert res.json()["detail"] == (
        "Global reset has been replaced by session delete/export controls"
    )


def test_no_active_session_blocks_join(client):
    cfg = client.get("/api/config").json()
    assert cfg["join_open"] is False
    assert cfg["active_session_id"] is None

    res = client.post("/api/join", json={"name": "Mira", "access_code": "ROOM1"})

    assert res.status_code == 409
    assert res.json()["detail"] == "No active session"


def create_session(client, name="Morning", access_code="ROOM1", activate=False):
    res = client.post(
        "/api/admin/sessions",
        json={"name": name, "access_code": access_code},
        auth=ADMIN,
        headers=ACTION,
    )
    assert res.status_code == 200
    session_id = res.json()["id"]
    if activate:
        active = client.post(
            f"/api/admin/sessions/{session_id}/activate",
            auth=ADMIN,
            headers=ACTION,
        )
        assert active.status_code == 200
    return session_id


def join_session(client, name="Mira", access_code="ROOM1"):
    res = client.post("/api/join", json={"name": name, "access_code": access_code})
    assert res.status_code == 200
    return res.json()


def ask_question(client, token, text):
    res = client.post(
        "/api/question",
        json={"text": text},
        headers={"X-Token": token},
    )
    assert res.status_code == 200
    return res.json()


def session_with_participant(
    session_id,
    session_name,
    access_code,
    participant_name,
    token,
):
    return {
        "id": session_id,
        "name": session_name,
        "access_code": access_code,
        "created_at": 10.0,
        "participants": {
            participant_name: {
                "token": token,
                "checks": {exercise: False for exercise in app_module.EXERCISES},
                "joined_at": 11.0,
            }
        },
        "questions": [],
        "feedback": [],
    }


def load_state_sequence(monkeypatch, *states):
    queue = list(states)

    def fake_load_state():
        assert queue
        return copy.deepcopy(queue.pop(0))

    monkeypatch.setattr(app_module, "load_state", fake_load_state)


def test_join_uses_only_active_session_access_code(client):
    session_id = create_session(client, access_code="ROOM1", activate=True)

    wrong = client.post("/api/join", json={"name": "Mira", "access_code": "ROOM2"})
    assert wrong.status_code == 401

    joined = join_session(client, name="Mira", access_code="ROOM1")
    assert joined["session_id"] == session_id
    assert joined["session_name"] == "Morning"
    assert joined["exercises"] == app_module.EXERCISES

    state = client.get(f"/api/admin/sessions/{session_id}/state", auth=ADMIN)
    assert state.status_code == 200
    assert [row["name"] for row in state.json()["rows"]] == ["Mira"]


def test_name_uniqueness_is_scoped_per_session_and_switch_invalidates_old_token(client):
    first_id = create_session(client, name="Morning", access_code="ROOM1", activate=True)
    first = join_session(client, name="Mira", access_code="ROOM1")
    duplicate = client.post("/api/join", json={"name": "Mira", "access_code": "ROOM1"})
    assert duplicate.status_code == 409

    second_id = create_session(client, name="Afternoon", access_code="ROOM2", activate=True)
    second = join_session(client, name="Mira", access_code="ROOM2")

    assert second["session_id"] == second_id
    assert first_id != second_id
    assert client.get("/api/me", headers={"X-Token": first["token"]}).status_code == 401
    assert client.get("/api/me", headers={"X-Token": second["token"]}).status_code == 200


def test_switched_session_old_token_cannot_update_same_named_participant(
    client, monkeypatch
):
    first = session_with_participant("s1", "Morning", "ROOM1", "Mira", "tok-first")
    second = session_with_participant("s2", "Afternoon", "ROOM2", "Mira", "tok-second")
    stale_state = {"active_session_id": "s1", "sessions": {"s1": first, "s2": second}}
    current_state = {"active_session_id": "s2", "sessions": {"s1": first, "s2": second}}
    saved = []
    load_state_sequence(monkeypatch, stale_state, current_state)
    monkeypatch.setattr(app_module, "save_state", lambda state: saved.append(state))

    res = client.post(
        "/api/check",
        json={"exercise": app_module.EXERCISES[0], "done": True},
        headers={"X-Token": "tok-first"},
    )

    assert res.status_code == 401
    assert saved == []


def test_switched_session_old_token_cannot_read_same_named_participant_questions(
    client, monkeypatch
):
    first = session_with_participant("s1", "Morning", "ROOM1", "Mira", "tok-first")
    second = session_with_participant("s2", "Afternoon", "ROOM2", "Mira", "tok-second")
    second["questions"].append(
        {
            "id": "q1",
            "name": "Mira",
            "text": "Second session only?",
            "ts": 12.0,
            "answered": False,
            "votes": {},
            "reply": "",
        }
    )
    stale_state = {"active_session_id": "s1", "sessions": {"s1": first, "s2": second}}
    current_state = {"active_session_id": "s2", "sessions": {"s1": first, "s2": second}}
    load_state_sequence(monkeypatch, stale_state, current_state)

    res = client.get("/api/questions", headers={"X-Token": "tok-first"})

    assert res.status_code == 401


def test_progress_questions_votes_and_feedback_are_session_scoped(client):
    first_id = create_session(client, name="Morning", access_code="ROOM1", activate=True)
    mira = join_session(client, name="Mira", access_code="ROOM1")
    headers = {"X-Token": mira["token"]}
    check = client.post(
        "/api/check",
        json={"exercise": "Setup", "done": True},
        headers=headers,
    )
    assert check.status_code == 200
    question = client.post(
        "/api/question",
        json={"text": "First question"},
        headers=headers,
    )
    assert question.status_code == 200
    vote = client.post(
        "/api/vote",
        json={"id": question.json()["id"], "vote": 1},
        headers=headers,
    )
    assert vote.status_code == 200
    feedback = client.post(
        "/api/feedback",
        json={"text": "First feedback"},
        headers=headers,
    )
    assert feedback.status_code == 200

    second_id = create_session(client, name="Afternoon", access_code="ROOM2", activate=True)
    nora = join_session(client, name="Nora", access_code="ROOM2")
    second_headers = {"X-Token": nora["token"]}

    assert client.get("/api/questions", headers=headers).status_code == 401
    assert client.get("/api/questions", headers=second_headers).json()["questions"] == []

    first_state = client.get(f"/api/admin/sessions/{first_id}/state", auth=ADMIN).json()
    second_state = client.get(f"/api/admin/sessions/{second_id}/state", auth=ADMIN).json()
    assert first_state["counts"]["Setup"] == 1
    assert [q["text"] for q in first_state["questions"]] == ["First question"]
    assert [f["text"] for f in first_state["feedback"]] == ["First feedback"]
    assert second_state["counts"]["Setup"] == 0
    assert second_state["questions"] == []
    assert second_state["feedback"] == []


def test_admin_reply_can_target_inactive_session(client):
    first_id = create_session(client, name="Morning", access_code="ROOM1", activate=True)
    mira = join_session(client, name="Mira", access_code="ROOM1")
    question_id = ask_question(client, mira["token"], "Can you repeat that?")["id"]
    second_id = create_session(client, name="Afternoon", access_code="ROOM2", activate=True)
    nora = join_session(client, name="Nora", access_code="ROOM2")
    active_question_id = ask_question(client, nora["token"], "Active question")["id"]

    reply = client.post(
        "/api/admin/reply",
        json={
            "id": question_id,
            "reply": "Yes, after the break.",
            "session_id": first_id,
        },
        auth=ADMIN,
        headers=ACTION,
    )

    assert reply.status_code == 200
    assert reply.json()["reply"] == "Yes, after the break."
    assert reply.json()["answered"] is True
    first_state = client.get(f"/api/admin/sessions/{first_id}/state", auth=ADMIN).json()
    second_state = client.get(f"/api/admin/sessions/{second_id}/state", auth=ADMIN).json()
    assert first_state["questions"][0]["reply"] == "Yes, after the break."
    assert first_state["questions"][0]["answered"] is True
    assert len(second_state["questions"]) == 1
    assert second_state["questions"][0]["id"] == active_question_id
    assert second_state["questions"][0]["reply"] == ""
    assert second_state["questions"][0]["answered"] is False


def test_admin_answer_can_target_inactive_session(client):
    first_id = create_session(client, name="Morning", access_code="ROOM1", activate=True)
    mira = join_session(client, name="Mira", access_code="ROOM1")
    question_id = ask_question(client, mira["token"], "Will this be recorded?")["id"]
    second_id = create_session(client, name="Afternoon", access_code="ROOM2", activate=True)
    nora = join_session(client, name="Nora", access_code="ROOM2")
    active_question_id = ask_question(client, nora["token"], "Active question")["id"]

    answer = client.post(
        "/api/admin/answer",
        json={"id": question_id, "answered": True, "session_id": first_id},
        auth=ADMIN,
        headers=ACTION,
    )

    assert answer.status_code == 200
    first_state = client.get(f"/api/admin/sessions/{first_id}/state", auth=ADMIN).json()
    second_state = client.get(f"/api/admin/sessions/{second_id}/state", auth=ADMIN).json()
    assert first_state["questions"][0]["answered"] is True
    assert len(second_state["questions"]) == 1
    assert second_state["questions"][0]["id"] == active_question_id
    assert second_state["questions"][0]["answered"] is False


def test_admin_reply_blank_session_id_is_rejected_without_mutating_active_session(client):
    create_session(client, name="Morning", access_code="ROOM1", activate=True)
    mira = join_session(client, name="Mira", access_code="ROOM1")
    question_id = ask_question(client, mira["token"], "Can you repeat that?")["id"]

    reply = client.post(
        "/api/admin/reply",
        json={"id": question_id, "reply": "No mutation", "session_id": "   "},
        auth=ADMIN,
        headers=ACTION,
    )

    assert reply.status_code == 422
    assert reply.json()["detail"] == "Session id is required"
    questions = client.get("/api/admin/state", auth=ADMIN).json()["questions"]
    assert questions[0]["reply"] == ""
    assert questions[0]["answered"] is False


def test_admin_answer_blank_session_id_is_rejected_without_mutating_active_session(client):
    create_session(client, name="Morning", access_code="ROOM1", activate=True)
    mira = join_session(client, name="Mira", access_code="ROOM1")
    question_id = ask_question(client, mira["token"], "Will this be recorded?")["id"]

    answer = client.post(
        "/api/admin/answer",
        json={"id": question_id, "answered": True, "session_id": ""},
        auth=ADMIN,
        headers=ACTION,
    )

    assert answer.status_code == 422
    assert answer.json()["detail"] == "Session id is required"
    questions = client.get("/api/admin/state", auth=ADMIN).json()["questions"]
    assert questions[0]["answered"] is False


def test_admin_reply_unknown_session_id_returns_not_found(client):
    create_session(client, name="Morning", access_code="ROOM1", activate=True)
    mira = join_session(client, name="Mira", access_code="ROOM1")
    question_id = ask_question(client, mira["token"], "Can you repeat that?")["id"]

    reply = client.post(
        "/api/admin/reply",
        json={"id": question_id, "reply": "No mutation", "session_id": "missing"},
        auth=ADMIN,
        headers=ACTION,
    )

    assert reply.status_code == 404
    assert reply.json()["detail"] == "Unknown session id"
    questions = client.get("/api/admin/state", auth=ADMIN).json()["questions"]
    assert questions[0]["reply"] == ""
    assert questions[0]["answered"] is False


def test_admin_answer_unknown_session_id_returns_not_found(client):
    create_session(client, name="Morning", access_code="ROOM1", activate=True)
    mira = join_session(client, name="Mira", access_code="ROOM1")
    question_id = ask_question(client, mira["token"], "Will this be recorded?")["id"]

    answer = client.post(
        "/api/admin/answer",
        json={"id": question_id, "answered": True, "session_id": "missing"},
        auth=ADMIN,
        headers=ACTION,
    )

    assert answer.status_code == 404
    assert answer.json()["detail"] == "Unknown session id"
    questions = client.get("/api/admin/state", auth=ADMIN).json()["questions"]
    assert questions[0]["answered"] is False


def test_join_rejects_duplicate_names_case_insensitively_after_trimming(client):
    create_session(client, access_code="ROOM1", activate=True)
    joined = join_session(client, name="  Mira  ", access_code="ROOM1")

    duplicate = client.post(
        "/api/join",
        json={"name": "mira", "access_code": "ROOM1"},
    )

    assert joined["name"] == "Mira"
    assert duplicate.status_code == 409


def test_admin_session_lifecycle(client):
    session_id = create_session(client, name="Morning", access_code="ROOM1")

    listed = client.get("/api/admin/sessions", auth=ADMIN).json()
    assert listed["active_session_id"] is None
    assert listed["sessions"][0]["id"] == session_id
    assert listed["sessions"][0]["active"] is False

    edited = client.patch(
        f"/api/admin/sessions/{session_id}",
        json={"name": "Afternoon", "access_code": "ROOM2"},
        auth=ADMIN,
        headers=ACTION,
    )
    assert edited.status_code == 200
    assert edited.json()["name"] == "Afternoon"
    assert edited.json()["access_code"] == "ROOM2"
    listed_after_edit = client.get("/api/admin/sessions", auth=ADMIN).json()
    assert listed_after_edit["sessions"][0]["id"] == session_id
    assert listed_after_edit["sessions"][0]["name"] == "Afternoon"
    assert listed_after_edit["sessions"][0]["access_code"] == "ROOM2"

    activated = client.post(
        f"/api/admin/sessions/{session_id}/activate",
        auth=ADMIN,
        headers=ACTION,
    )
    assert activated.status_code == 200
    assert activated.json()["active"] is True
    assert client.get("/api/config").json()["active_session_id"] == session_id

    deactivated = client.post(
        "/api/admin/sessions/deactivate",
        auth=ADMIN,
        headers=ACTION,
    )
    assert deactivated.status_code == 200
    assert client.get("/api/config").json()["active_session_id"] is None


def test_delete_active_session_clears_active_id(client):
    session_id = create_session(client, activate=True)

    deleted = client.delete(
        f"/api/admin/sessions/{session_id}",
        auth=ADMIN,
        headers=ACTION,
    )

    assert deleted.status_code == 200
    assert client.get("/api/admin/sessions", auth=ADMIN).json() == {
        "active_session_id": None,
        "sessions": [],
    }
    assert client.get("/api/config").json()["join_open"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "   ", "access_code": "ROOM1"},
        {"name": "A" * 81, "access_code": "ROOM1"},
        {"name": "Morning", "access_code": "   "},
        {"name": "Morning", "access_code": "A" * 81},
    ],
)
def test_admin_create_session_rejects_invalid_name_or_access_code(client, payload):
    res = client.post(
        "/api/admin/sessions",
        json=payload,
        auth=ADMIN,
        headers=ACTION,
    )

    assert res.status_code == 422
    assert client.get("/api/admin/sessions", auth=ADMIN).json() == {
        "active_session_id": None,
        "sessions": [],
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "   ", "access_code": "ROOM2"},
        {"name": "A" * 81, "access_code": "ROOM2"},
        {"name": "Afternoon", "access_code": "   "},
        {"name": "Afternoon", "access_code": "A" * 81},
    ],
)
def test_admin_update_session_rejects_invalid_name_or_access_code(client, payload):
    session_id = create_session(client, name="Morning", access_code="ROOM1")

    res = client.patch(
        f"/api/admin/sessions/{session_id}",
        json=payload,
        auth=ADMIN,
        headers=ACTION,
    )

    assert res.status_code == 422
    persisted = app_module.load_state()["sessions"][session_id]
    assert persisted["name"] == "Morning"
    assert persisted["access_code"] == "ROOM1"


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "Afternoon"},
        {"access_code": "ROOM2"},
    ],
)
def test_admin_update_session_requires_full_name_and_access_code_payload(
    client, payload
):
    session_id = create_session(client, name="Morning", access_code="ROOM1")

    res = client.patch(
        f"/api/admin/sessions/{session_id}",
        json=payload,
        auth=ADMIN,
        headers=ACTION,
    )

    assert res.status_code == 422
    persisted = app_module.load_state()["sessions"][session_id]
    assert persisted["name"] == "Morning"
    assert persisted["access_code"] == "ROOM1"


def test_admin_session_unknown_id_returns_404_for_state_changes(client):
    patched = client.patch(
        "/api/admin/sessions/missing",
        json={"name": "Afternoon", "access_code": "ROOM2"},
        auth=ADMIN,
        headers=ACTION,
    )
    activated = client.post(
        "/api/admin/sessions/missing/activate",
        auth=ADMIN,
        headers=ACTION,
    )
    deleted = client.delete(
        "/api/admin/sessions/missing",
        auth=ADMIN,
        headers=ACTION,
    )

    assert patched.status_code == 404
    assert patched.json()["detail"] == "Unknown session id"
    assert activated.status_code == 404
    assert activated.json()["detail"] == "Unknown session id"
    assert deleted.status_code == 404
    assert deleted.json()["detail"] == "Unknown session id"


def test_admin_session_state_changes_require_admin_action_header(client):
    session_id = create_session(client, name="Morning", access_code="ROOM1")

    responses = [
        client.post(
            "/api/admin/sessions",
            json={"name": "Afternoon", "access_code": "ROOM2"},
            auth=ADMIN,
        ),
        client.post(
            "/api/admin/sessions/deactivate",
            auth=ADMIN,
        ),
        client.patch(
            f"/api/admin/sessions/{session_id}",
            json={"name": "Afternoon", "access_code": "ROOM2"},
            auth=ADMIN,
        ),
        client.post(
            f"/api/admin/sessions/{session_id}/activate",
            auth=ADMIN,
        ),
        client.delete(
            f"/api/admin/sessions/{session_id}",
            auth=ADMIN,
        ),
    ]

    for res in responses:
        assert res.status_code == 403
        assert res.json()["detail"] == "Missing X-Admin-Action header"


def test_export_and_import_one_session_file(client):
    session_id = create_session(
        client, name="Morning Session", access_code="ROOM1", activate=True
    )
    joined = join_session(client, name="Mira", access_code="ROOM1")
    headers = {"X-Token": joined["token"]}
    client.post("/api/check", json={"exercise": "Setup", "done": True}, headers=headers)
    client.post("/api/question", json={"text": "Export me"}, headers=headers)
    client.post("/api/feedback", json={"text": "Keep me"}, headers=headers)

    exported = client.get(f"/api/admin/sessions/{session_id}/export", auth=ADMIN)

    assert exported.status_code == 200
    assert "attachment" in exported.headers["content-disposition"]
    payload = exported.json()
    assert payload["format"] == "workshop-progress-session"
    assert payload["version"] == 1
    assert "id" not in payload["session"]
    assert payload["session"]["name"] == "Morning Session"
    assert payload["session"]["access_code"] == "ROOM1"
    assert payload["session"]["participants"]["Mira"]["checks"]["Setup"] is True
    assert payload["session"]["questions"][0]["text"] == "Export me"
    assert payload["session"]["feedback"][0]["text"] == "Keep me"

    imported = client.post(
        "/api/admin/sessions/import",
        json=payload,
        auth=ADMIN,
        headers=ACTION,
    )

    assert imported.status_code == 200
    imported_body = imported.json()
    assert imported_body["id"] != session_id
    assert imported_body["active"] is False
    imported_state = client.get(
        f"/api/admin/sessions/{imported_body['id']}/state",
        auth=ADMIN,
    ).json()
    assert imported_state["rows"][0]["name"] == "Mira"
    assert imported_state["counts"]["Setup"] == 1
    assert imported_state["questions"][0]["text"] == "Export me"
    assert imported_state["feedback"][0]["text"] == "Keep me"


def test_invalid_import_does_not_modify_state(client):
    create_session(client, name="Morning", access_code="ROOM1")
    before = client.get("/api/admin/sessions", auth=ADMIN).json()

    res = client.post(
        "/api/admin/sessions/import",
        json={"format": "wrong", "version": 1, "session": {}},
        auth=ADMIN,
        headers=ACTION,
    )

    assert res.status_code == 422
    assert client.get("/api/admin/sessions", auth=ADMIN).json() == before


def test_activate_rejects_session_without_access_code(client):
    # Legacy migration yields an inactive session with an empty access code,
    # which the activate endpoint must refuse (the admin UI's "Activate selected"
    # button relies on this guard so an unconfigured session can't go live).
    write_state({"participants": {}, "questions": [], "feedback": []})
    listed = client.get("/api/admin/sessions", auth=ADMIN).json()
    assert len(listed["sessions"]) == 1
    session_id = listed["sessions"][0]["id"]
    assert listed["sessions"][0]["access_code"] == ""
    assert listed["sessions"][0]["active"] is False

    res = client.post(
        f"/api/admin/sessions/{session_id}/activate",
        auth=ADMIN,
        headers=ACTION,
    )

    assert res.status_code == 422
    assert res.json()["detail"] == "Access code is required"
    assert client.get("/api/config").json()["active_session_id"] is None
    after = client.get("/api/admin/sessions", auth=ADMIN).json()
    assert after["active_session_id"] is None
    assert after["sessions"][0]["active"] is False
    assert after["sessions"][0]["access_code"] == ""


def test_activating_second_session_switches_active_away_from_first(client):
    first_id = create_session(client, name="Morning", access_code="ROOM1", activate=True)
    second_id = create_session(client, name="Afternoon", access_code="ROOM2")

    listed = client.get("/api/admin/sessions", auth=ADMIN).json()
    assert listed["active_session_id"] == first_id
    by_id = {s["id"]: s for s in listed["sessions"]}
    assert by_id[first_id]["active"] is True
    assert by_id[second_id]["active"] is False

    activated = client.post(
        f"/api/admin/sessions/{second_id}/activate",
        auth=ADMIN,
        headers=ACTION,
    )

    assert activated.status_code == 200
    assert activated.json()["active"] is True
    cfg = client.get("/api/config").json()
    assert cfg["active_session_id"] == second_id
    assert cfg["session_name"] == "Afternoon"
    listed_after = client.get("/api/admin/sessions", auth=ADMIN).json()
    assert listed_after["active_session_id"] == second_id
    by_id_after = {s["id"]: s for s in listed_after["sessions"]}
    assert by_id_after[second_id]["active"] is True
    assert by_id_after[first_id]["active"] is False


def test_activate_already_active_session_is_idempotent(client):
    session_id = create_session(client, name="Morning", access_code="ROOM1", activate=True)

    res = client.post(
        f"/api/admin/sessions/{session_id}/activate",
        auth=ADMIN,
        headers=ACTION,
    )

    assert res.status_code == 200
    assert res.json()["active"] is True
    assert client.get("/api/config").json()["active_session_id"] == session_id
    listed = client.get("/api/admin/sessions", auth=ADMIN).json()
    assert listed["active_session_id"] == session_id
    assert listed["sessions"][0]["active"] is True


def remove_participant(client, session_id, name):
    return client.post(
        f"/api/admin/sessions/{session_id}/participants/remove",
        json={"name": name},
        auth=ADMIN,
        headers=ACTION,
    )


def test_remove_participant_drops_row_and_token_but_keeps_their_questions_votes_feedback(
    client,
):
    session_id = create_session(client, name="Morning", access_code="ROOM1", activate=True)
    mira = join_session(client, name="Mira", access_code="ROOM1")
    nora = join_session(client, name="Nora", access_code="ROOM1")
    mira_headers = {"X-Token": mira["token"]}
    nora_headers = {"X-Token": nora["token"]}
    question = ask_question(client, mira["token"], "Stays after I leave?")
    client.post(
        "/api/vote",
        json={"id": question["id"], "vote": 1},
        headers=nora_headers,
    )
    client.post("/api/feedback", json={"text": "Keep my feedback"}, headers=mira_headers)
    client.post(
        "/api/check",
        json={"exercise": "Setup", "done": True},
        headers=mira_headers,
    )

    removed = remove_participant(client, session_id, "Mira")

    assert removed.status_code == 200
    assert removed.json()["participant_count"] == 1
    # The removed participant's token is gone.
    assert client.get("/api/me", headers=mira_headers).status_code == 401
    # The other participant is untouched.
    assert client.get("/api/me", headers=nora_headers).status_code == 200
    # Question, votes, and feedback are preserved.
    nora_view = client.get("/api/questions", headers=nora_headers).json()["questions"]
    assert [q["text"] for q in nora_view] == ["Stays after I leave?"]
    assert nora_view[0]["score"] == 1
    admin_state = client.get(f"/api/admin/sessions/{session_id}/state", auth=ADMIN).json()
    assert [row["name"] for row in admin_state["rows"]] == ["Nora"]
    assert admin_state["questions"][0]["name"] == "Mira"
    assert [f["text"] for f in admin_state["feedback"]] == ["Keep my feedback"]


def test_remove_participant_works_for_inactive_session(client):
    session_id = create_session(client, name="Morning", access_code="ROOM1", activate=True)
    mira = join_session(client, name="Mira", access_code="ROOM1")
    client.post(
        "/api/admin/sessions/deactivate",
        auth=ADMIN,
        headers=ACTION,
    )

    removed = remove_participant(client, session_id, "Mira")

    assert removed.status_code == 200
    assert removed.json()["participant_count"] == 0
    assert removed.json()["active"] is False
    admin_state = client.get(f"/api/admin/sessions/{session_id}/state", auth=ADMIN).json()
    assert admin_state["rows"] == []


def test_remove_participant_trims_and_matches_stored_name(client):
    session_id = create_session(client, access_code="ROOM1", activate=True)
    join_session(client, name="Mira", access_code="ROOM1")

    removed = remove_participant(client, session_id, "  Mira  ")

    assert removed.status_code == 200
    assert removed.json()["participant_count"] == 0


def test_remove_participant_unknown_name_returns_not_found(client):
    session_id = create_session(client, access_code="ROOM1", activate=True)
    join_session(client, name="Mira", access_code="ROOM1")

    removed = remove_participant(client, session_id, "Nora")

    assert removed.status_code == 404
    assert removed.json()["detail"] == "Unknown participant"
    assert client.get(f"/api/admin/sessions/{session_id}/state", auth=ADMIN).json()["rows"][0]["name"] == "Mira"


def test_remove_participant_unknown_session_returns_not_found(client):
    removed = remove_participant(client, "missing", "Mira")

    assert removed.status_code == 404
    assert removed.json()["detail"] == "Unknown session id"


def test_remove_participant_requires_admin_action_header(client):
    session_id = create_session(client, access_code="ROOM1", activate=True)
    join_session(client, name="Mira", access_code="ROOM1")

    removed = client.post(
        f"/api/admin/sessions/{session_id}/participants/remove",
        json={"name": "Mira"},
        auth=ADMIN,
    )

    assert removed.status_code == 403
    assert removed.json()["detail"] == "Missing X-Admin-Action header"
    assert client.get(f"/api/admin/sessions/{session_id}/state", auth=ADMIN).json()["rows"][0]["name"] == "Mira"
