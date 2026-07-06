"""Workshop progress tracker.

Participants join the active admin-selected session with a name and session
access code. Each participant can only see and edit their own checklist
(enforced by a per-name token issued at join). The admin sees session-scoped
tables and can export, import, activate, deactivate, or delete sessions.

Configuration (environment variables):
    ADMIN_PASSWORD  required, password for the admin page (user: admin)
    WORKSHOP_TITLE  optional, title shown on both pages
    EXERCISES       optional, comma-separated column names
    DATA_FILE       optional, path to the JSON state file (default /data/progress.json)
    MAX_FEEDBACK_PER_PERSON  optional, per-participant feedback cap (default 20)
"""

import asyncio
import json
import os
import re
import secrets
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import FileResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

EXERCISES = [
    e.strip()
    for e in os.environ.get(
        "EXERCISES", "Setup,Ex 1,Ex 2,Ex 3,Ex 4,Ex 5,Ex 6"
    ).split(",")
    if e.strip()
]
DATA_FILE = Path(os.environ.get("DATA_FILE", "/data/progress.json"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")
WORKSHOP_TITLE = os.environ.get("WORKSHOP_TITLE", "Workshop Progress")
MAX_PARTICIPANTS = int(os.environ.get("MAX_PARTICIPANTS", "200"))
MAX_QUESTIONS_PER_PERSON = int(os.environ.get("MAX_QUESTIONS_PER_PERSON", "20"))
MAX_FEEDBACK_PER_PERSON = int(os.environ.get("MAX_FEEDBACK_PER_PERSON", "20"))
SESSION_EXPORT_FORMAT = "workshop-progress-session"
SESSION_EXPORT_VERSION = 1
IMPORTED_SESSION_NAME = "Imported workshop"
MAX_SESSION_NAME_LEN = 80
MAX_ACCESS_CODE_LEN = 80

app = FastAPI(title="Workshop Progress Tracker")
security = HTTPBasic()
lock = asyncio.Lock()

STATIC_DIR = Path(__file__).parent / "static"


def collapse_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def validate_session_name(value: str) -> str:
    name = collapse_spaces(value)
    if not (1 <= len(name) <= MAX_SESSION_NAME_LEN):
        raise HTTPException(422, "Session name must be 1-80 characters")
    return name


def validate_access_code(value: str, *, required: bool = True) -> str:
    code = value.strip()
    if required and not code:
        raise HTTPException(422, "Access code is required")
    if len(code) > MAX_ACCESS_CODE_LEN:
        raise HTTPException(422, "Access code must be 1-80 characters")
    return code


def empty_state() -> dict:
    return {"active_session_id": None, "sessions": {}}


def normalize_question(q: dict) -> dict:
    q.setdefault("votes", {})
    q.setdefault("reply", "")
    q.setdefault("answered", False)
    return q


def normalize_participant_row(row: dict, now: float) -> dict:
    row.setdefault("token", secrets.token_hex(16))
    checks = row.get("checks") if isinstance(row.get("checks"), dict) else {}
    normalized_checks = {str(exercise): bool(done) for exercise, done in checks.items()}
    for exercise in EXERCISES:
        normalized_checks.setdefault(exercise, False)
    row["checks"] = normalized_checks
    row["joined_at"] = float(row.get("joined_at") or now)
    if "feedback_count" in row:
        row["feedback_count"] = int(row.get("feedback_count") or 0)
    return row


def normalize_session(session: dict, session_id: str | None = None) -> dict:
    now = time.time()
    sid = session_id or session.get("id") or secrets.token_hex(8)
    name = session.get("name") if isinstance(session.get("name"), str) else ""
    access_code = (
        session.get("access_code") if isinstance(session.get("access_code"), str) else ""
    )
    participants = (
        session.get("participants") if isinstance(session.get("participants"), dict) else {}
    )
    questions = session.get("questions") if isinstance(session.get("questions"), list) else []
    feedback = session.get("feedback") if isinstance(session.get("feedback"), list) else []
    session["id"] = sid
    session["name"] = collapse_spaces(name)[:MAX_SESSION_NAME_LEN] or IMPORTED_SESSION_NAME
    session["access_code"] = access_code.strip()[:MAX_ACCESS_CODE_LEN]
    session["created_at"] = float(session.get("created_at") or now)
    session["participants"] = {
        str(name): normalize_participant_row(dict(row), now)
        for name, row in participants.items()
        if isinstance(row, dict)
    }
    session["questions"] = [
        normalize_question(dict(q)) for q in questions if isinstance(q, dict)
    ]
    session["feedback"] = [dict(f) for f in feedback if isinstance(f, dict)]
    return session


def migrate_legacy_state(raw: dict) -> dict:
    sid = secrets.token_hex(8)
    session = normalize_session(
        {
            "id": sid,
            "name": IMPORTED_SESSION_NAME,
            "access_code": "",
            "created_at": time.time(),
            "participants": raw.get("participants", {}),
            "questions": raw.get("questions", []),
            "feedback": raw.get("feedback", []),
        },
        session_id=sid,
    )
    return {"active_session_id": None, "sessions": {sid: session}}


def load_state() -> dict:
    if not DATA_FILE.exists():
        return empty_state()
    raw = json.loads(DATA_FILE.read_text())
    if "sessions" not in raw:
        state = migrate_legacy_state(raw)
        save_state(state)
        return state
    state = empty_state()
    sessions = raw.get("sessions") if isinstance(raw.get("sessions"), dict) else {}
    for sid, session in sessions.items():
        normalized = normalize_session(dict(session), session_id=str(sid))
        state["sessions"][normalized["id"]] = normalized
    active_id = raw.get("active_session_id")
    state["active_session_id"] = active_id if active_id in state["sessions"] else None
    return state


def get_active_session(state: dict) -> dict | None:
    active_id = state.get("active_session_id")
    if not active_id:
        return None
    return state["sessions"].get(active_id)


def get_session_or_404(state: dict, session_id: str) -> dict:
    session = state["sessions"].get(session_id)
    if not session:
        raise HTTPException(404, "Unknown session id")
    return session


def active_session_for_participant(state: dict, session_id: str, name: str) -> dict:
    if state.get("active_session_id") != session_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown or missing token")
    session = state["sessions"].get(session_id)
    if not session or name not in session["participants"]:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown or missing token")
    return session


def session_summary(session: dict, active_session_id: str | None) -> dict:
    questions = session["questions"]
    return {
        "id": session["id"],
        "name": session["name"],
        "access_code": session["access_code"],
        "created_at": session["created_at"],
        "active": session["id"] == active_session_id,
        "participant_count": len(session["participants"]),
        "question_count": len(questions),
        "open_question_count": sum(1 for q in questions if not q.get("answered")),
        "feedback_count": len(session["feedback"]),
    }


def safe_filename(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-")
    return slug.lower() or "session"


def session_export_payload(session: dict) -> dict:
    return {
        "format": SESSION_EXPORT_FORMAT,
        "version": SESSION_EXPORT_VERSION,
        "session": {
            "name": session["name"],
            "access_code": session["access_code"],
            "created_at": session["created_at"],
            "participants": session["participants"],
            "questions": session["questions"],
            "feedback": session["feedback"],
        },
    }


def imported_session_from_payload(payload: "SessionImportRequest") -> dict:
    if (
        payload.format != SESSION_EXPORT_FORMAT
        or payload.version != SESSION_EXPORT_VERSION
    ):
        raise HTTPException(422, "Invalid session export")
    data = payload.session
    name = data.get("name")
    access_code = data.get("access_code")
    participants = data.get("participants")
    questions = data.get("questions")
    feedback = data.get("feedback")
    if not isinstance(name, str):
        raise HTTPException(422, "Invalid session export")
    if not isinstance(access_code, str):
        raise HTTPException(422, "Invalid session export")
    if not isinstance(participants, dict):
        raise HTTPException(422, "Invalid session export")
    if not isinstance(questions, list):
        raise HTTPException(422, "Invalid session export")
    if not isinstance(feedback, list):
        raise HTTPException(422, "Invalid session export")
    if any(not isinstance(q, dict) for q in questions):
        raise HTTPException(422, "Invalid session export")
    if any(not isinstance(f, dict) for f in feedback):
        raise HTTPException(422, "Invalid session export")
    session_id = secrets.token_hex(8)
    return normalize_session(
        {
            "id": session_id,
            "name": validate_session_name(name),
            "access_code": validate_access_code(access_code, required=False),
            "created_at": float(data.get("created_at") or time.time()),
            "participants": participants,
            "questions": questions,
            "feedback": feedback,
        },
        session_id=session_id,
    )


def empty_board() -> dict:
    return {
        "exercises": EXERCISES,
        "rows": [],
        "counts": {exercise: 0 for exercise in EXERCISES},
        "questions": [],
        "feedback": [],
    }


def session_board(session: dict) -> dict:
    rows = [
        {"name": name, "checks": row["checks"], "joined_at": row["joined_at"]}
        for name, row in sorted(
            session["participants"].items(), key=lambda kv: kv[1]["joined_at"]
        )
    ]
    counts = {
        exercise: sum(1 for row in rows if row["checks"].get(exercise))
        for exercise in EXERCISES
    }
    questions = [question_view(q) for q in session["questions"]]
    questions.sort(key=lambda q: (q["answered"], -q["score"], q["ts"]))
    return {
        "exercises": EXERCISES,
        "rows": rows,
        "counts": counts,
        "questions": questions,
        "feedback": list(reversed(session["feedback"])),
    }


def board_state(session: dict | None) -> dict:
    if not session:
        return empty_board()
    return session_board(session)


def admin_target_session(state: dict, session_id: str | None) -> dict:
    if session_id is not None:
        target_id = session_id.strip()
        if not target_id:
            raise HTTPException(422, "Session id is required")
        return get_session_or_404(state, target_id)
    session = get_active_session(state)
    if not session:
        raise HTTPException(404, "No active session")
    return session


def question_view(q: dict, viewer: str | None = None) -> dict:
    """Public shape of a question: votes aggregated to a score."""
    view = {
        "id": q["id"],
        "name": q["name"],
        "text": q["text"],
        "ts": q["ts"],
        "answered": q["answered"],
        "reply": q["reply"],
        "score": sum(q["votes"].values()),
    }
    if viewer is not None:
        view["my_vote"] = q["votes"].get(viewer, 0)
    return view


def save_state(state: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(DATA_FILE)


def require_admin(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    if not ADMIN_PASSWORD:
        raise HTTPException(500, "ADMIN_PASSWORD is not configured")
    ok = credentials.username == "admin" and secrets.compare_digest(
        credentials.password, ADMIN_PASSWORD
    )
    if not ok:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Wrong credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


def require_admin_action(
    credentials: HTTPBasicCredentials = Depends(security),
    x_admin_action: str = Header(default=""),
) -> None:
    """Admin auth for state-changing endpoints.

    The custom header blocks CSRF via cached Basic credentials: a cross-site
    form cannot set it, and a cross-origin fetch that tries dies in the CORS
    preflight.
    """
    require_admin(credentials)
    if x_admin_action != "1":
        raise HTTPException(403, "Missing X-Admin-Action header")


async def require_participant(x_token: str = Header(default="")) -> tuple[str, dict, str]:
    async with lock:
        state = load_state()
    session = get_active_session(state)
    if not session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown or missing token")
    for name, row in session["participants"].items():
        if x_token and secrets.compare_digest(row["token"], x_token):
            return name, row, session["id"]
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown or missing token")


class JoinRequest(BaseModel):
    name: str
    access_code: str = ""


class SessionRequest(BaseModel):
    name: str
    access_code: str


class SessionImportRequest(BaseModel):
    format: str
    version: int
    session: dict


class CheckRequest(BaseModel):
    exercise: str
    done: bool


class QuestionRequest(BaseModel):
    text: str


class FeedbackRequest(BaseModel):
    text: str


class VoteRequest(BaseModel):
    id: str
    vote: int  # 1 = up, -1 = down, 0 = remove my vote


class AnswerRequest(BaseModel):
    id: str
    answered: bool
    session_id: str | None = None


class ReplyRequest(BaseModel):
    id: str
    reply: str
    session_id: str | None = None


class RemoveParticipantRequest(BaseModel):
    name: str


@app.get("/api/config")
async def config():
    async with lock:
        state = load_state()
    session = get_active_session(state)
    return {
        "title": WORKSHOP_TITLE,
        "active_session_id": session["id"] if session else None,
        "session_name": session["name"] if session else "",
        "join_open": session is not None,
        "access_code_required": bool(session and session["access_code"]),
    }


@app.get("/api/admin/sessions")
async def admin_sessions(_=Depends(require_admin)):
    async with lock:
        state = load_state()
    sessions = [
        session_summary(session, state["active_session_id"])
        for session in state["sessions"].values()
    ]
    sessions.sort(key=lambda s: (not s["active"], s["created_at"], s["name"].lower()))
    return {"active_session_id": state["active_session_id"], "sessions": sessions}


@app.post("/api/admin/sessions")
async def admin_create_session(req: SessionRequest, _=Depends(require_admin_action)):
    name = validate_session_name(req.name)
    access_code = validate_access_code(req.access_code)
    session_id = secrets.token_hex(8)
    session = normalize_session(
        {
            "id": session_id,
            "name": name,
            "access_code": access_code,
            "created_at": time.time(),
            "participants": {},
            "questions": [],
            "feedback": [],
        },
        session_id=session_id,
    )
    async with lock:
        state = load_state()
        while session_id in state["sessions"]:
            session_id = secrets.token_hex(8)
            session["id"] = session_id
        state["sessions"][session_id] = session
        save_state(state)
    return session_summary(session, state["active_session_id"])


@app.post("/api/admin/sessions/deactivate")
async def admin_deactivate_sessions(_=Depends(require_admin_action)):
    async with lock:
        state = load_state()
        state["active_session_id"] = None
        save_state(state)
    return {"active_session_id": None}


@app.post("/api/admin/sessions/import")
async def admin_import_session(
    req: SessionImportRequest,
    _=Depends(require_admin_action),
):
    session = imported_session_from_payload(req)
    async with lock:
        state = load_state()
        while session["id"] in state["sessions"]:
            session["id"] = secrets.token_hex(8)
        state["sessions"][session["id"]] = session
        save_state(state)
    return session_summary(session, state["active_session_id"])


@app.get("/api/admin/sessions/{session_id}/export")
async def admin_export_session(session_id: str, _=Depends(require_admin)):
    async with lock:
        state = load_state()
        session = get_session_or_404(state, session_id)
        payload = session_export_payload(session)
    filename = f"workshop-session-{safe_filename(session['name'])}.json"
    return Response(
        json.dumps(payload, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.patch("/api/admin/sessions/{session_id}")
async def admin_update_session(
    session_id: str,
    req: SessionRequest,
    _=Depends(require_admin_action),
):
    name = validate_session_name(req.name)
    access_code = validate_access_code(req.access_code)
    async with lock:
        state = load_state()
        session = get_session_or_404(state, session_id)
        session["name"] = name
        session["access_code"] = access_code
        save_state(state)
    return session_summary(session, state["active_session_id"])


@app.post("/api/admin/sessions/{session_id}/activate")
async def admin_activate_session(session_id: str, _=Depends(require_admin_action)):
    async with lock:
        state = load_state()
        session = get_session_or_404(state, session_id)
        if not session["access_code"]:
            raise HTTPException(422, "Access code is required")
        state["active_session_id"] = session_id
        save_state(state)
    return session_summary(session, state["active_session_id"])


@app.get("/api/admin/sessions/{session_id}/state")
async def admin_session_state(session_id: str, _=Depends(require_admin)):
    async with lock:
        state = load_state()
    return board_state(get_session_or_404(state, session_id))


@app.delete("/api/admin/sessions/{session_id}")
async def admin_delete_session(session_id: str, _=Depends(require_admin_action)):
    async with lock:
        state = load_state()
        get_session_or_404(state, session_id)
        del state["sessions"][session_id]
        if state["active_session_id"] == session_id:
            state["active_session_id"] = None
        save_state(state)
    return {"ok": True}


@app.post("/api/admin/sessions/{session_id}/participants/remove")
async def admin_remove_participant(
    session_id: str,
    req: RemoveParticipantRequest,
    _=Depends(require_admin_action),
):
    name = collapse_spaces(req.name)
    if not name:
        raise HTTPException(422, "Name is required")
    async with lock:
        state = load_state()
        session = get_session_or_404(state, session_id)
        if name not in session["participants"]:
            raise HTTPException(404, "Unknown participant")
        # Drop only the participant's row and token. Their questions, votes,
        # and anonymous feedback stay in the session unmodified.
        del session["participants"][name]
        save_state(state)
    return session_summary(session, state["active_session_id"])


@app.post("/api/join")
async def join(req: JoinRequest):
    name = collapse_spaces(req.name)
    if not (1 <= len(name) <= 40):
        raise HTTPException(422, "Name must be 1-40 characters")
    async with lock:
        state = load_state()
        session = get_active_session(state)
        if not session:
            raise HTTPException(409, "No active session")
        if not secrets.compare_digest(req.access_code.strip(), session["access_code"]):
            raise HTTPException(401, "Wrong access code — it is on the setup slide")
        if len(session["participants"]) >= MAX_PARTICIPANTS:
            raise HTTPException(409, "Workshop is full")
        taken = {n.lower() for n in session["participants"]}
        if name.lower() in taken:
            raise HTTPException(409, "Name already taken — pick another one")
        token = secrets.token_hex(16)
        session["participants"][name] = {
            "token": token,
            "checks": {e: False for e in EXERCISES},
            "joined_at": time.time(),
        }
        save_state(state)
    return {
        "name": name,
        "token": token,
        "session_id": session["id"],
        "session_name": session["name"],
        "exercises": EXERCISES,
    }


@app.get("/api/me")
async def me(participant=Depends(require_participant)):
    name, row, session_id = participant
    async with lock:
        state = load_state()
        session = active_session_for_participant(state, session_id, name)
        row = session["participants"][name]
    return {
        "name": name,
        "session_id": session_id,
        "session_name": session["name"],
        "exercises": EXERCISES,
        "checks": row["checks"],
    }


@app.post("/api/check")
async def check(req: CheckRequest, participant=Depends(require_participant)):
    name, _, session_id = participant
    if req.exercise not in EXERCISES:
        raise HTTPException(422, f"Unknown exercise: {req.exercise}")
    async with lock:
        state = load_state()
        session = active_session_for_participant(state, session_id, name)
        session["participants"][name]["checks"][req.exercise] = req.done
        save_state(state)
    return {"ok": True}


@app.post("/api/question")
async def ask_question(req: QuestionRequest, participant=Depends(require_participant)):
    name, _, session_id = participant
    text = collapse_spaces(req.text)
    if not (1 <= len(text) <= 500):
        raise HTTPException(422, "Question must be 1-500 characters")
    question = {
        "id": secrets.token_hex(8),
        "name": name,
        "text": text,
        "ts": time.time(),
        "answered": False,
        "votes": {},
        "reply": "",
    }
    async with lock:
        state = load_state()
        session = active_session_for_participant(state, session_id, name)
        mine = sum(1 for q in session["questions"] if q["name"] == name)
        if mine >= MAX_QUESTIONS_PER_PERSON:
            raise HTTPException(429, "Question limit reached — flag a helper instead")
        session["questions"].append(question)
        save_state(state)
    return question_view(question, viewer=name)


@app.get("/api/questions")
async def list_questions(participant=Depends(require_participant)):
    name, _, session_id = participant
    async with lock:
        state = load_state()
    session = active_session_for_participant(state, session_id, name)
    questions = [question_view(q, viewer=name) for q in session["questions"]]
    questions.sort(key=lambda q: (q["answered"], -q["score"], q["ts"]))
    return {"questions": questions}


@app.post("/api/vote")
async def vote(req: VoteRequest, participant=Depends(require_participant)):
    name, _, session_id = participant
    if req.vote not in (-1, 0, 1):
        raise HTTPException(422, "vote must be -1, 0, or 1")
    async with lock:
        state = load_state()
        session = active_session_for_participant(state, session_id, name)
        for q in session["questions"]:
            if q["id"] == req.id:
                if req.vote == 0:
                    q["votes"].pop(name, None)
                else:
                    q["votes"][name] = req.vote
                save_state(state)
                return question_view(q, viewer=name)
    raise HTTPException(404, "Unknown question id")


@app.post("/api/feedback")
async def send_feedback(req: FeedbackRequest, participant=Depends(require_participant)):
    """Anonymous feedback: token required as a spam guard, name not stored."""
    name, _, session_id = participant
    text = collapse_spaces(req.text)
    if not (1 <= len(text) <= 500):
        raise HTTPException(422, "Feedback must be 1-500 characters")
    async with lock:
        state = load_state()
        session = active_session_for_participant(state, session_id, name)
        row = session["participants"][name]
        if row.get("feedback_count", 0) >= MAX_FEEDBACK_PER_PERSON:
            raise HTTPException(429, "Feedback limit reached — talk to the instructor directly")
        row["feedback_count"] = row.get("feedback_count", 0) + 1
        session["feedback"].append(
            {"id": secrets.token_hex(8), "text": text, "ts": time.time()}
        )
        save_state(state)
    return {"ok": True}


@app.post("/api/admin/reply")
async def admin_reply(req: ReplyRequest, _=Depends(require_admin_action)):
    reply = req.reply.strip()
    if len(reply) > 2000:
        raise HTTPException(422, "Reply too long (max 2000 characters)")
    async with lock:
        state = load_state()
        session = admin_target_session(state, req.session_id)
        for q in session["questions"]:
            if q["id"] == req.id:
                q["reply"] = reply
                if reply:
                    q["answered"] = True
                save_state(state)
                return question_view(q)
    raise HTTPException(404, "Unknown question id")


@app.post("/api/admin/answer")
async def admin_answer(req: AnswerRequest, _=Depends(require_admin_action)):
    async with lock:
        state = load_state()
        session = admin_target_session(state, req.session_id)
        for q in session["questions"]:
            if q["id"] == req.id:
                q["answered"] = req.answered
                save_state(state)
                return {"ok": True}
    raise HTTPException(404, "Unknown question id")


@app.get("/api/admin/state")
async def admin_state(_=Depends(require_admin)):
    async with lock:
        state = load_state()
    return board_state(get_active_session(state))


@app.post("/api/admin/reset")
async def admin_reset(_=Depends(require_admin_action)):
    raise HTTPException(
        status.HTTP_410_GONE,
        "Global reset has been replaced by session delete/export controls",
    )


@app.get("/admin")
async def admin_page(_=Depends(require_admin)):
    return FileResponse(STATIC_DIR / "admin.html")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
