# Deployment Notes — Vercel & the Read-Only Filesystem Problem

> **Status: KNOWN-LIMITED.** The backend currently *boots and serves requests*
> on Vercel via a `/tmp` stopgap, but **runtime state does not reliably
> persist** there. This is fine for a demo; it is **not** production-ready.
> Read "The Proper Fix" before relying on this for real users.

_Last updated: 2026-06-11._

---

## TL;DR

BioAPEX is a **file-first** backend (per `CLAUDE.md`: sessions, memory, and
artifacts live on disk; there is intentionally **no database**). That design
assumes a **writable, persistent filesystem**.

**Vercel serverless functions do not provide one.** The deployment bundle at
`/var/task` is **read-only**, and the only writable location (`/tmp`) is
**ephemeral** (wiped on cold start) and **not shared** between concurrent
function instances.

We applied a stopgap that redirects the backend's pure-output directories
(`sessions/`, `storage/`, `artifacts/`) to `/tmp/bioapex` so the app boots and
can answer a request. State written there will not survive across cold starts
or scale-out.

---

## The errors we hit (in the order they surfaced)

Each fix peeled back one layer of the *same* root cause — a read-only FS under
a file-first app. Timeline of crashes seen in the Vercel function logs:

1. **`FUNCTION_INVOCATION_FAILED` — skills snapshot write**
   `scan_skills()` tried to write `SKILLS_SNAPSHOT.md` into the read-only
   bundle during FastAPI's lifespan startup, crashing the function before it
   could serve anything.
   **Fix:** wrapped `scan_skills()` in `app.py` in try/except (non-fatal); the
   pre-generated `SKILLS_SNAPSHOT.md` is committed so it's readable at runtime.

2. **401 / 403 on every request — loopback auth**
   Vercel routes through an internal proxy that adds `x-forwarded-for`, so the
   "loopback bypass" in `access_control.py` never fired and all requests were
   rejected (no bearer token configured).
   **Fix:** committed `backend/config.json` with
   `production_hardening.api.trust_forwarded_loopback_headers: true` and added
   the prod origin to `cors_allowed_origins`. (Also un-ignored `config.json`
   and `SKILLS_SNAPSHOT.md` in `.gitignore` so they actually deploy.)

3. **500 — Pydantic `extra_forbidden` ValidationError**
   `RuntimeConfigModel` (in `runtime_config_types.py`) was missing four fields
   that exist in `config.py`'s `_DEFAULT`: `max_turn_wallclock_s`,
   `tool_wallclock`, `verification.verifier_max_wall_s`,
   `verification.verifier_max_tokens`. Vercel's vendored Pydantic enforced
   `extra="forbid"` strictly and crashed at import time.
   **Fix:** added the four missing field declarations to the models.
   **Lesson:** `_DEFAULT` in `config.py` and the Pydantic models in
   `runtime_config_types.py` must stay in sync — adding a config default
   without a matching model field will crash on a strict Pydantic build.

4. **500 — `OSError: Read-only file system: '/var/task/sessions'`**
   `SessionStore.__init__` called `mkdir()` on `base_dir/sessions`.
   **Fix (stopgap):** redirect pure-output trees to `/tmp` — see below.

---

## The `/tmp` stopgap (what's implemented now)

New module **`backend/runtime_paths.py`** exposes `resolve_data_dir(base_dir)`:

- Honors `BIOAPEX_DATA_DIR` env var if set (explicit override).
- Returns `base_dir` unchanged when it is **writable** (local dev, persistent
  hosts) — so local behavior is identical to before.
- Falls back to **`/tmp/bioapex`** when `base_dir` is read-only (Vercel).

It is applied **only at the write chokepoints of pure-output trees**, leaving
all resource *reads* (`skills/`, `workspace/`, `knowledge/`, `memory/` content,
`SKILLS_SNAPSHOT.md`, `config.json`) on `base_dir`:

| Subsystem | File | What redirects |
|---|---|---|
| Sessions | `graph/session/session_store.py` | `sessions/` |
| Memory index | `graph/memory_indexer.py` | `storage/memory_index/` (content still read from `base_dir/memory`) |
| Subagent artifacts | `runtime/subagent.py` | `artifacts/subagent/...` |

### Known gaps still present under the stopgap

- **No persistence across requests.** `/tmp` is per-instance and ephemeral.
  A session created in one request may be gone on the next (cold start or a
  different instance). Multi-turn conversations may break.
- **Memory writes** (`graph/memory_writer.py`) still target
  `base_dir/memory` (a read-only resource dir on Vercel). The agent's
  "save memory" tool will fail there; tool dispatch catches the error and
  returns it to the model rather than 500'ing, so it degrades gracefully but
  does not work. Not redirected because memory is *both* a read resource and a
  write target (the genuinely hard case).
- **LLM not wired on the deploy.** Logs showed
  `LLM is explicitly disabled. Using MockLLM.` Confirm the backend service in
  Vercel has non-empty `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` /
  `DEEPSEEK_MODEL` (and `OPENAI_API_KEY` for embeddings). Without these the
  chat returns mock responses. **This is separate from the filesystem issue.**
- **Audit / artifact-registry writes** are soft (wrapped in `except`), so they
  silently no-op on read-only FS rather than persisting.

---

## The Proper Fix (do this when investing in real users)

The file-first architecture wants a **long-running process with a persistent,
writable disk**. Vercel serverless gives you neither. Options, best first:

1. **Move the backend off Vercel; keep the frontend on Vercel.**
   Deploy the FastAPI backend to a host with a persistent volume and a
   always-on process:
   - **Render** (Web Service + Persistent Disk), **Railway** (volume),
     **Fly.io** (volume), or a small VM (Hetzner / DigitalOcean / EC2).
   - Point the frontend's `NEXT_PUBLIC_API_URL` at the new backend URL.
   - Remove the `/tmp` stopgap (or leave it — it's a no-op on a writable host)
     and drop `trust_forwarded_loopback_headers` in favor of a real
     **bearer-token** auth posture (`trusted-lab` / `hosted-strict` in
     `hardening.py`), since the backend will now be publicly reachable.
   - Mount the persistent disk at the backend project root (or set
     `BIOAPEX_DATA_DIR` to the mounted volume path).

2. **Keep Vercel but externalize state** (larger rewrite, fights the
   architecture). Swap on-disk persistence for managed services:
   sessions/memory → a database or KV (e.g. Postgres, Upstash Redis),
   artifacts → object storage (e.g. S3/R2). This contradicts the file-first
   design in `CLAUDE.md` ("there is no database — do not introduce one"), so
   only do this if you deliberately want to change that principle.

**Recommendation:** Option 1. It preserves the architecture, is the least
code, and gives you real persistence.

---

## Files involved (for the cleanup later)

- `backend/runtime_paths.py` — the `/tmp` redirect helper (new).
- `backend/graph/session/session_store.py` — sessions redirect.
- `backend/graph/memory_indexer.py` — index storage redirect.
- `backend/runtime/subagent.py` — subagent artifact redirect.
- `backend/app.py` — `scan_skills()` made non-fatal.
- `backend/config.json` — CORS + forwarded-loopback trust (committed).
- `backend/runtime_config_types.py` — the four missing Pydantic fields.
- `vercel.json` — frontend + backend service config (`/_/backend` route).
- `.gitignore` — un-ignored `config.json` and `SKILLS_SNAPSHOT.md`.

When migrating to a persistent host, revisit `trust_forwarded_loopback_headers`
(it's a security loosening that only made sense because Vercel's proxy looked
non-loopback) and set up proper bearer-token auth instead.
