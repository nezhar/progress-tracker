# Workshop Progress Tracker

Tiny self-hosted "are we done yet?" board for the Prompt Engineering Hands-on
workshop.

- **Participants** open the root page, type a name once, and get a personal
  checklist (Setup, Ex 1 … Ex 6). They see and edit only their own list — a
  token issued at join time and kept in `localStorage` enforces that. A shared
  questions panel sits next to the checklist: everyone sees all questions and
  can upvote/downvote them (one vote per person per question, toggleable).
  A button below the board opens a dialog to ask a question or send feedback —
  feedback is anonymous and visible only to the admin.
- **Admin** opens `/admin` (HTTP Basic, user `admin`), sees the full table
  with live done-counts per exercise plus all questions sorted by votes (open
  ones first), and can reset everything. Each question takes a written reply —
  participants see it under the question, and saving a non-empty reply marks
  the question answered. Handy during exercise time: answer in text while the
  room works, pick the top-voted rest up verbally.
- State lives in one JSON file on a volume. No database, no accounts.

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
| `PARTICIPANT_PASSCODE` | empty (open) | Shared join code, e.g. shown on a slide |
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

- "Reset all" wipes participants and checks; participants get a fresh join
  screen on their next interaction (their stored token becomes invalid).
- If someone loses their row (new browser, cleared storage), they just join
  again with a slightly different name.
- Facilitation rule of thumb: continue when a column reaches ~75% of joined
  participants; announce "60 more seconds" instead of waiting for 100%.
