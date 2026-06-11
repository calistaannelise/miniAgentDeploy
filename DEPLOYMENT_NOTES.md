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

2. **403 on every gated request — loopback auth fails on serverless**
   `access_control.py` grants unauthenticated access only to *loopback*
   clients (`request.client.host` is `127.0.0.1`/`::1`). On Vercel the request
   arrives through the platform proxy, so `is_loopback_client()` is always
   False and the gate rejects everything with
   `"This route requires local access or a configured bearer token."`
   **Important:** an early attempt set
   `trust_forwarded_loopback_headers: true` — this does **not** help, because
   that flag is only consulted *after* loopback is already detected, which
   never happens on Vercel. It was removed.
   **Real fix:** configure a **bearer token** (see "Bearer-token auth" below).
   Also committed `backend/config.json` and added the prod origin to
   `cors_allowed_origins`. (And un-ignored `config.json` and
   `SKILLS_SNAPSHOT.md` in `.gitignore` so they actually deploy.)

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

5. **Frontend "chat is unavailable" — two frontend bugs in `api.ts`**
   a. `buildApiUrl` used `new URL(path, base)`. Because API paths are absolute
   (`/api/chat`), the `URL` constructor *drops* the base's path, turning
   `https://bioapex.vercel.app/_/backend` + `/api/chat` into
   `https://bioapex.vercel.app/api/chat` (the `/_/backend` mount vanished).
   **Fix:** build the URL by concatenation (`base + path`) so the mount prefix
   is preserved.
   b. `getBase()` only fell back to `http://<hostname>:8002` (a dead port on
   Vercel) when `NEXT_PUBLIC_API_URL` was absent — and `NEXT_PUBLIC_*` vars are
   inlined at **build time**, so a var set after/around the build was missing
   from the shipped JS.
   **Fix:** `getBase()` now falls back to same-origin `${origin}/_/backend`
   when deployed, so the frontend reaches the backend even without the env var.
   Verified the `405` on `GET /_/backend/api/chat` afterwards — route resolves;
   405 just means chat is POST-only. (`GET /_/backend/api` → 404 is normal:
   there is no bare `/api` route.)

6. **Frontend API calls blocked by Vercel Deployment Protection — NOT our code**
   After the URL fixes, `fetch()` calls to `/_/backend/...` returned **401 with
   an HTML body** and a `set-cookie: _vercel_sso_nonce=...` header. That is
   **Vercel Deployment Protection** ("Vercel Authentication") intercepting the
   request at the edge and returning its login page **before the request ever
   reaches the FastAPI backend**. Our backend returns *JSON* 401s
   (`{"detail": "..."}`); an *HTML* 401 + `_vercel_sso_nonce` cookie is always
   Vercel's gate, not ours.

   **Why it blocks you even though you're a project member:** Deployment
   Protection doesn't check "is this user a member?" per request — it checks
   "does this request carry a valid Vercel auth session?" You get that session
   only through an **interactive login redirect**, which only happens on
   **top-level page navigation** (so the page loads fine). A background
   `fetch()` **cannot** follow an interactive login redirect — it just receives
   the login page as data and fails with 401. So a logged-in human can load the
   page, but the page's own API calls are still blocked. This is a known
   incompatibility between Deployment Protection and the
   "SPA calls its own protected backend" pattern.

   Symptom tell-tales: response `content-type: text/html` (not JSON),
   `set-cookie: _vercel_sso_nonce`, and it happens on every API call
   regardless of bearer token.

   **Fix:** Vercel Dashboard → Project → **Settings → Deployment Protection** →
   set **Vercel Authentication** to **Disabled**, Save, redeploy. The backend's
   own **bearer-token** auth (below) then becomes the real access control for
   the now-public backend — keep it enabled, since the `dev` posture leaves
   code-execution tools on. (Vercel's "Protection Bypass for Automation" token
   is *not* a usable alternative here: it would have to be embedded in the
   public frontend JS, which protects nothing — same effect as disabling.)

   **Note on preview URLs:** Deployment Protection defaults to ON for *preview*
   deployments, and each push gets a fresh preview URL (a new origin). Combined
   with bearer tokens being stored in `localStorage` keyed by origin, this
   means re-entering the token on every preview. **Test on the stable
   production URL (`https://bioapex.vercel.app`)** and confirm protection is off
   there too.

---

## Bearer-token auth (current access model on Vercel)

Because loopback never works on serverless (bug #2), gated routes require a
**bearer token** on Vercel. Local dev is unaffected — it still uses the
loopback bypass and needs no token.

**Config** (`backend/config.json`): all three scopes point at one env var so a
single token value authorizes everything:

```json
"api": {
  "allow_loopback_without_auth": true,
  "inspection_bearer_token_env_var": "BIOAPEX_API_TOKEN",
  "execution_bearer_token_env_var": "BIOAPEX_API_TOKEN",
  "admin_bearer_token_env_var": "BIOAPEX_API_TOKEN"
}
```

**Setup checklist:**
1. Set `BIOAPEX_API_TOKEN=<secret>` on the **backend** service in Vercel, then
   redeploy. (Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.)
2. In the app's navbar access panel, paste the same value into the
   **Inspection** and **Execution** token fields (Admin optional) and click
   **Apply Tokens**. The token persists in browser localStorage per-domain.

**Probe responses & what they mean:** `200` granted ✓ · `503` env var unset in
Vercel (or not redeployed) · `401` UI token ≠ Vercel value · `403` old
deployment without the new `config.json`.

**How auth resolves** (`access_control.py::determine_route_access_mode`): try
loopback bypass (fails on Vercel) → look up the scope's token env var → `503`
if the env var is empty → `401` if the presented `Authorization: Bearer …`
header doesn't match → otherwise grant `"bearer"`.

> **Future:** when the backend moves to a persistent host (see "The Proper
> Fix"), keep bearer-token auth (or move to a stronger posture in
> `hardening.py`) since the backend remains publicly reachable. The agent has
> code-execution tools enabled under the `dev` posture — do **not** switch to
> unauthenticated/open access on a public URL, or a visitor could have the
> agent read secrets (e.g. `DEEPSEEK_API_KEY`) out of the environment.

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
   - Point the frontend's `NEXT_PUBLIC_API_URL` at the new backend URL (and/or
     update the deployed-fallback origin in `getBase()` if it's not same-origin
     anymore).
   - Remove the `/tmp` stopgap (or leave it — it's a no-op on a writable host).
     Keep the **bearer-token** auth (or move to a stronger posture —
     `trusted-lab` / `hosted-strict` in `hardening.py`), since the backend
     stays publicly reachable.
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
- `backend/config.json` — CORS + bearer-token env var names (committed).
- `backend/runtime_config_types.py` — the four missing Pydantic fields.
- `frontend/src/lib/api.ts` — URL-prefix fix + same-origin `/_/backend`
  fallback in `getBase()`.
- `vercel.json` — frontend + backend service config (`/_/backend` route).
- `.gitignore` — un-ignored `config.json` and `SKILLS_SNAPSHOT.md`.

When migrating to a persistent host, keep bearer-token auth (the backend stays
publicly reachable) and reconsider the `dev` posture, which leaves
code-execution tools enabled.
