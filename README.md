# Workshop Progress Tracker

Tiny self-hosted "are we done yet?" board for the Prompt Engineering Hands-on
workshop.

- **Participants** can join only when an admin has marked a session active.
  They enter their name plus the active session's access code. Their checklist,
  questions, votes, and feedback belong to that session. Each participant sees
  and edits only their own checklist (a per-name token issued at join enforces
  this). A shared questions panel sits next to the checklist: everyone sees all
  questions in the session and can upvote/downvote them (one vote per person
  per question, toggleable). A button below the board opens a dialog to ask a
  question or send feedback — feedback is anonymous and visible only to admin.
- **Admin** opens `/admin` (HTTP Basic, user `admin`), creates sessions with
  access codes, selects which session to inspect, marks one session active or
  leaves all sessions inactive, exports one session JSON file, imports one
  session JSON file as inactive, and deletes whole sessions when they are no
  longer needed. The selected session's table shows live done-counts per
  exercise plus questions sorted by votes (open ones first); each question
  takes a written reply that participants see under the question (saving a
  non-empty reply marks it answered).
- State lives in one JSON file on a volume. No database, no accounts.

## Sessions

Sessions preserve workshop data between runs. New installs start with no active
session, so participants see a closed message until an admin creates and
activates a session. Existing pre-session data is migrated into one inactive
session named "Imported workshop".

Export downloads one JSON file for the selected session. Import accepts that
one-session JSON format, assigns a fresh internal id, and keeps the imported
session inactive until an admin activates it.

**Run exactly one container / one worker.** Concurrency safety comes from a
single process serializing all writes behind an in-process lock (plus atomic
file replace). Do not add `--workers N` or scale to multiple replicas — with
more than one process the lock no longer protects the file.

## Run

```bash
cp .env.example .env   # then set ADMIN_PASSWORD
docker compose up -d --build
```

Or without compose:

```bash
docker build -t progress-tracker .
docker run -d --name progress-tracker \
  -p 8000:8000 \
  -v progress-data:/data \
  -e ADMIN_PASSWORD='choose-something' \
  progress-tracker
```

- Participants: `http://<host>:8000/`
- Admin board: `http://<host>:8000/admin`

Put it behind your usual reverse proxy with TLS; HTTP Basic sends the
password with every request, so only use the admin page over HTTPS.

## Configuration

Set via `.env` (see `.env.example`; compose reads it automatically):

| Variable | Default | Purpose |
| --- | --- | --- |
| `ADMIN_PASSWORD` | — (required) | Password for `/admin` and the admin API |
| `PARTICIPANT_PASSCODE` | empty | Deprecated; session access codes are managed on `/admin` |
| `WORKSHOP_TITLE` | `Workshop Progress` | Title shown on both pages |
| `PORT` | `8000` | Host port published by compose |
| `EXERCISES` | `Setup,Ex 1,Ex 2,Ex 3,Ex 4,Ex 5,Ex 6` | Checklist columns |
| `MAX_PARTICIPANTS` | `200` | Join cap (abuse guard) |
| `MAX_QUESTIONS_PER_PERSON` | `20` | Question cap per participant |
| `MAX_FEEDBACK_PER_PERSON` | `20` | Feedback cap per participant |
| `DATA_FILE` | `/data/progress.json` | State file location |

## Embedding as an iframe

Add `?embed=1` — the tracker hides its own title, drops page padding/background,
and reports its content height to the parent via `postMessage`:

```html
<iframe id="tracker" src="https://tracker.example.com/?embed=1"
        style="width:100%; border:0" title="Workshop progress"></iframe>
<script>
  const tracker = document.getElementById("tracker");
  window.addEventListener("message", (event) => {
    if (event.source !== tracker.contentWindow) return;
    if (event.data?.type !== "progress-tracker:height") return;
    tracker.style.height = event.data.height + "px";
  });
</script>
```

Note on cross-site storage: browsers partition third-party localStorage, so a
participant who joins inside the embed has a separate identity from a direct
visit (and strict privacy modes may not persist it at all — the join then lasts
for the tab session).

## Workshop notes

- "No active session" closes joining without deleting data.
- Deleting a session removes that session's participants, checks, questions,
  and feedback. Export first if you want to keep a copy.
- Activating a different session invalidates tokens issued under the previous
  active session; participants get a fresh join screen on their next interaction.
- If someone loses their row (new browser, cleared storage), they just join
  again with a slightly different name.
- Facilitation rule of thumb: continue when a column reaches ~75% of joined
  participants; announce "60 more seconds" instead of waiting for 100%.
