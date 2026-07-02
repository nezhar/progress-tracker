"""Workshop progress tracker.

Participants join with a name and check off exercises; each participant can
only see and edit their own checklist (enforced by a per-name token issued at
join). The admin sees the full table and can reset it.

Configuration (environment variables):
    ADMIN_PASSWORD  required, password for the admin page (user: admin)
    PARTICIPANT_PASSCODE  optional, shared code required to join (empty = open)
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
from fastapi.responses import FileResponse
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
PARTICIPANT_PASSCODE = os.environ.get("PARTICIPANT_PASSCODE", "").strip()
MAX_PARTICIPANTS = int(os.environ.get("MAX_PARTICIPANTS", "200"))
MAX_QUESTIONS_PER_PERSON = int(os.environ.get("MAX_QUESTIONS_PER_PERSON", "20"))
MAX_FEEDBACK_PER_PERSON = int(os.environ.get("MAX_FEEDBACK_PER_PERSON", "20"))

app = FastAPI(title="Workshop Progress Tracker")
security = HTTPBasic()
lock = asyncio.Lock()

STATIC_DIR = Path(__file__).parent / "static"


def load_state() -> dict:
    if DATA_FILE.exists():
        state = json.loads(DATA_FILE.read_text())
        state.setdefault("questions", [])
        state.setdefault("feedback", [])
        for q in state["questions"]:
            q.setdefault("votes", {})
            q.setdefault("reply", "")
        return state
    return {"participants": {}, "questions": [], "feedback": []}


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


async def require_participant(x_token: str = Header(default="")) -> tuple[str, dict]:
    async with lock:
        state = load_state()
    for name, row in state["participants"].items():
        if x_token and secrets.compare_digest(row["token"], x_token):
            return name, row
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown or missing token")


class JoinRequest(BaseModel):
    name: str
    passcode: str = ""


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


class ReplyRequest(BaseModel):
    id: str
    reply: str


@app.get("/api/config")
async def config():
    return {
        "title": WORKSHOP_TITLE,
        "passcode_required": bool(PARTICIPANT_PASSCODE),
    }


@app.post("/api/join")
async def join(req: JoinRequest):
    if PARTICIPANT_PASSCODE and not secrets.compare_digest(
        req.passcode.strip(), PARTICIPANT_PASSCODE
    ):
        raise HTTPException(401, "Wrong passcode — it is on the setup slide")
    name = re.sub(r"\s+", " ", req.name).strip()
    if not (1 <= len(name) <= 40):
        raise HTTPException(422, "Name must be 1-40 characters")
    async with lock:
        state = load_state()
        if len(state["participants"]) >= MAX_PARTICIPANTS:
            raise HTTPException(409, "Workshop is full")
        taken = {n.lower() for n in state["participants"]}
        if name.lower() in taken:
            raise HTTPException(409, "Name already taken — pick another one")
        token = secrets.token_hex(16)
        state["participants"][name] = {
            "token": token,
            "checks": {e: False for e in EXERCISES},
            "joined_at": time.time(),
        }
        save_state(state)
    return {"name": name, "token": token, "exercises": EXERCISES}


@app.get("/api/me")
async def me(participant=Depends(require_participant)):
    name, row = participant
    return {"name": name, "exercises": EXERCISES, "checks": row["checks"]}


@app.post("/api/check")
async def check(req: CheckRequest, participant=Depends(require_participant)):
    name, _ = participant
    if req.exercise not in EXERCISES:
        raise HTTPException(422, f"Unknown exercise: {req.exercise}")
    async with lock:
        state = load_state()
        state["participants"][name]["checks"][req.exercise] = req.done
        save_state(state)
    return {"ok": True}


@app.post("/api/question")
async def ask_question(req: QuestionRequest, participant=Depends(require_participant)):
    name, _ = participant
    text = re.sub(r"\s+", " ", req.text).strip()
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
        mine = sum(1 for q in state["questions"] if q["name"] == name)
        if mine >= MAX_QUESTIONS_PER_PERSON:
            raise HTTPException(429, "Question limit reached — flag a helper instead")
        state["questions"].append(question)
        save_state(state)
    return question_view(question, viewer=name)


@app.get("/api/questions")
async def list_questions(participant=Depends(require_participant)):
    name, _ = participant
    async with lock:
        state = load_state()
    questions = [question_view(q, viewer=name) for q in state["questions"]]
    questions.sort(key=lambda q: (q["answered"], -q["score"], q["ts"]))
    return {"questions": questions}


@app.post("/api/vote")
async def vote(req: VoteRequest, participant=Depends(require_participant)):
    name, _ = participant
    if req.vote not in (-1, 0, 1):
        raise HTTPException(422, "vote must be -1, 0, or 1")
    async with lock:
        state = load_state()
        for q in state["questions"]:
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
    name, _ = participant
    text = re.sub(r"\s+", " ", req.text).strip()
    if not (1 <= len(text) <= 500):
        raise HTTPException(422, "Feedback must be 1-500 characters")
    async with lock:
        state = load_state()
        row = state["participants"][name]
        if row.get("feedback_count", 0) >= MAX_FEEDBACK_PER_PERSON:
            raise HTTPException(429, "Feedback limit reached — talk to the instructor directly")
        row["feedback_count"] = row.get("feedback_count", 0) + 1
        state["feedback"].append(
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
        for q in state["questions"]:
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
        for q in state["questions"]:
            if q["id"] == req.id:
                q["answered"] = req.answered
                save_state(state)
                return {"ok": True}
    raise HTTPException(404, "Unknown question id")


@app.get("/api/admin/state")
async def admin_state(_=Depends(require_admin)):
    async with lock:
        state = load_state()
    rows = [
        {"name": name, "checks": row["checks"], "joined_at": row["joined_at"]}
        for name, row in sorted(
            state["participants"].items(), key=lambda kv: kv[1]["joined_at"]
        )
    ]
    counts = {
        e: sum(1 for r in rows if r["checks"].get(e)) for e in EXERCISES
    }
    questions = [question_view(q) for q in state["questions"]]
    questions.sort(key=lambda q: (q["answered"], -q["score"], q["ts"]))
    return {
        "exercises": EXERCISES,
        "rows": rows,
        "counts": counts,
        "questions": questions,
        "feedback": list(reversed(state["feedback"])),
    }


@app.post("/api/admin/reset")
async def admin_reset(_=Depends(require_admin_action)):
    async with lock:
        save_state({"participants": {}, "questions": [], "feedback": []})
    return {"ok": True}


@app.get("/admin")
async def admin_page(_=Depends(require_admin)):
    return FileResponse(STATIC_DIR / "admin.html")


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
