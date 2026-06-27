# GopherPath Deployment Runbook

Target: **Vercel** (frontend) + **Railway** (backend + PostgreSQL).

The code-side preparation in this repo is already done (see "What's already prepared"
below). The steps in this file are the account-gated actions that require *your*
Railway/Vercel logins — they can't be done from the repo alone.

---

## ⚠️ Do this first: scrub the student APAS PDF from git history

`My APAS - APAS Results Tab.pdf` (real student data) was committed in earlier history.
It is now untracked and `.gitignore`d, but it **still exists in past commits**. Before
you push this repo to a remote that anyone else can read, remove it from history:

```bash
# Option A: git-filter-repo (recommended; install via `brew install git-filter-repo`)
git filter-repo --path "My APAS - APAS Results Tab.pdf" --invert-paths

# Option B: keep the repo private and accept the historical blob.
```

`git filter-repo` rewrites history (new commit hashes). Do it before the first push,
or force-push after if the remote already has the old history. If this is your own
APAS and the repo stays private, Option B is acceptable.

---

## What's already prepared in this repo

- `requirements.txt` (root, pinned to validated versions)
- `Procfile` → `web: uvicorn backend.main:app --host 0.0.0.0 --port $PORT`
- `backend/main.py`: slowapi rate limiting (`/parse-apas` = 5/hour per IP,
  `/chat/{token}` = 50/day **per session token**) + CORS that allows localhost,
  `FRONTEND_ORIGIN`, and any `*.vercel.app` preview URL.
- `frontend/lib/api.ts`: `API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"`
- `.gitignore` fixed (the old `*.db*.pdf` line ignored nothing); `.env`, all `*.pdf`,
  and `parsed_apas.json` are now ignored. Course CSVs in `database/` stay **tracked**
  (they're the course-data transfer plan).
- `.env.example` documents required env vars.

Pre-deploy verification already passing locally:
- `SOUNDNESS_STUDENT_ID=17 python3 backend/test_plan_soundness.py` → all 8 combos, 9 rules
- `SOUNDNESS_STUDENT_ID=23 python3 backend/test_plan_soundness.py` → all 8 combos, 9 rules
- `python3 backend/test_optimizer_determinism.py` → identical across 5 runs, no TBD

---

## Backend — Railway

1. **Create project + Postgres**: New Project → Deploy from GitHub repo → add the
   **PostgreSQL** plugin. Railway injects `DATABASE_URL` automatically.
2. **Set environment variables** (Service → Variables):
   - `ANTHROPIC_API_KEY` = your Anthropic key
   - `FRONTEND_ORIGIN` = your Vercel production URL (fill in after the Vercel step;
     re-deploy is not needed since it's read at startup — just redeploy once known)
   - `DATABASE_URL` is provided by the Postgres plugin; reference it if needed.
3. Railway auto-detects the `Procfile`. Confirm the build uses `requirements.txt`.
4. **Load schema** (run locally, pointing at Railway's public DB URL — copy it from the
   Postgres plugin's "Connect" tab):
   ```bash
   psql "$RAILWAY_DATABASE_URL" -f database/schema.sql
   ```
5. **Import course data** (CSVs are committed, so run the scripts locally against
   Railway's DB — the import scripts read `DATABASE_URL` from the environment and the
   CSV path is hardcoded relative to repo root):
   ```bash
   DATABASE_URL="$RAILWAY_DATABASE_URL" python3 scrapers/import_courses.py
   DATABASE_URL="$RAILWAY_DATABASE_URL" python3 scrapers/import_le_requirements.py
   ```
6. **Verify row counts**:
   ```bash
   psql "$RAILWAY_DATABASE_URL" -c "SELECT COUNT(*) FROM courses;"            # ~14,239
   psql "$RAILWAY_DATABASE_URL" -c "SELECT COUNT(*) FROM course_attributes;"  # ~2,400+
   ```
7. **Health check**: `curl https://<railway-url>/` → `{"status":"ok","service":"GopherPath API"}`

---

## Frontend — Vercel

1. **Import the GitHub repo** in Vercel. **Set Root Directory = `frontend`** (the Next.js
   app is in a subfolder, not the repo root).
2. **Environment variable**: `NEXT_PUBLIC_API_URL` = your Railway backend URL
   (e.g. `https://gopherpath-backend.up.railway.app`, no trailing slash).
3. Deploy (auto on push, or `cd frontend && vercel --prod`).
4. Copy the resulting production URL back into Railway's `FRONTEND_ORIGIN` and redeploy
   the backend so CORS allows it explicitly. (Preview URLs already work via the
   `*.vercel.app` regex.)

---

## Post-deploy smoke test (run against the production URL, in order)

1. Upload an APAS PDF → parsing succeeds, returns a session token.
2. Pick a graduation term → correct number of semesters generated.
3. Generate a plan → real courses, **no TBD placeholders**.
4. AI explanation streams in the right column.
5. Send one chat message → responds with real, specific course codes.
6. Refresh → "Continue my plan" banner appears, full state restored without re-streaming.
7. Upload Ekin's APAS (BME), pick Spring 2028 → BMEN courses appear across 4 semesters.
8. Soundness against Railway's DB:
   ```bash
   DATABASE_URL="$RAILWAY_DATABASE_URL" SOUNDNESS_STUDENT_ID=<id> python3 backend/test_plan_soundness.py 2>/dev/null
   ```
9. Rate limiter: 6 rapid POSTs to `/parse-apas` from one IP → the 6th returns **429**.

---

## Notes / follow-ups

- In-memory rate limiting is fine for V1. If you scale to multiple Railway workers,
  the per-IP/per-session counters won't be shared across processes — switch to a
  Redis-backed `Limiter(storage_uri=...)` then.
- `FRONTEND_ORIGIN` is read at process start; set it before/at the redeploy that should
  enforce it.
