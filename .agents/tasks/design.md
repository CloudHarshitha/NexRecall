# Design: Integrate the custom HTML sign-in/sign-up page into NexRecall's Chainlit app

## Chosen architecture (one sentence)

**Option A** — a FastAPI/Starlette router (the existing `auth_router` in `auth.py`, spliced into `chainlit.server.app` before the catch-all) serves the polished `public/auth.html` at `/auth`, exposes `/api/auth/*`, and on successful login/register mints **Chainlit's native session: a PyJWT (HS256) token stored in an httpOnly cookie**, signed with the existing `CHAINLIT_AUTH_SECRET` env var via `chainlit.server.create_jwt` + `chainlit.server.set_auth_cookie`. Chainlit then reads that same cookie to authenticate the chat at `/`. No parallel session system, no new dependencies.

---

## 1. Overview

The repo already contains ~80% of the wiring this task needs. `auth.py` already defines `auth_router` with `POST /api/auth/login` and `POST /api/auth/register`, both of which already call `chainlit.server.create_jwt` + `set_auth_cookie` to set Chainlit's own auth cookie, and `app.py` already imports `auth` so those routes are spliced into `chainlit.server.app` before Chainlit's `GET /{full_path:path}` catch-all (via `_register_before_catchall()`). `database.py` already provides `AppUser`, `get_password_hash` (bcrypt), and `verify_password` (bcrypt). `app.py` already registers `@cl.password_auth_callback`, which keeps Chainlit's login "enabled" so unauthenticated visits to `/` are redirected to `/login`, and the existing `GET /login` route redirects to `/auth`.

So the work is **not greenfield**: it is (a) swap the old embedded `AUTH_PAGE_HTML` page for the new polished `public/auth.html`, (b) adapt the two existing endpoints to the new page's contract (`{ "message": ... }` errors, register payload without `confirm_password`, register auto-login), (c) add the three missing endpoints (`forgot-password`, `google`, `microsoft`), and (d) flip `DEMO: false` plus one small SSO wiring change in `public/auth.html`. The Chainlit chat flow is untouched and stays gated behind the custom page's session via the existing `/login` → `/auth` interception.

### Why Option A (and not B)

- Option B ("overlay Chainlit's built-in login via customization hooks") would fight Chainlit's React SPA: the built-in login is a server-rendered/React page we cannot pixel-faithfully replace with `auth.html` through `custom.js`/`custom_css` hooks. The repo's authors already chose Option A — intercept `GET /login` and serve a custom HTML page at `/auth` — and it works. Extending that pattern is the lowest-risk, most cohesive path.
- Option A keeps Chainlit's auth semantics (the JWT cookie is exactly what Chainlit mints itself), so the running chat needs **zero** changes to recognize a logged-in user. The custom page is purely an alternative login UI that produces the *same* session Chainlit's own login would.

---

## 2. Session mechanism (Decision 1 + Decision 4) — confirmed from code

Verified against the installed Chainlit 2.12.0 in `.venv312`:

- `chainlit.server.create_jwt(data: cl.User) -> str` encodes `data.to_dict()` plus `exp`/`iat` with **PyJWT HS256**, using `chainlit.auth.get_jwt_secret()`, which is literally `os.environ.get("CHAINLIT_AUTH_SECRET")`. `exp = config.project.user_session_timeout` = **1296000 s (15 days)** (from `.chainlit/config.toml`). It asserts the secret is present (login fails loudly if absent).
- `chainlit.server.set_auth_cookie(request, response, token)` sets an **httpOnly** cookie (chunked into `_auth_cookie_name_<i>` if the token exceeds 3000 chars), with `secure`/`samesite`/`path` taken from Chainlit's module-level cookie config, `max_age` = 15 days. It also deletes leftover chunks from any previously larger token.
- `CHAINLIT_AUTH_SECRET` is **already set in `.env`** (present, non-empty — value not reproduced here). `SESSION_SECRET` is also set but is **not used** by this design (Chainlit uses `CHAINLIT_AUTH_SECRET`).

Consequence: the session is Chainlit's own httpOnly PyJWT cookie. We reuse `create_jwt` + `set_auth_cookie` exactly as the existing `login_api` already does. No `itsdangerous` (not installed), no new JWT lib (PyJWT 2.15.1 ships with Chainlit), no new secret. **Production note:** `CHAINLIT_AUTH_SECRET` must remain set in any deployed environment (it already is); if it were missing, `create_jwt`'s `assert secret` would raise and login would 500 — a loud, correct failure.

Logout: Chainlit already exposes `POST /logout` (clears the auth cookie). The custom page has no logout button, so no new logout route is needed; the existing Chainlit logout remains available to the chat UI.

---

## 3. Code findings (cited — what the design builds on)

- **`auth.py`** — `auth_router = APIRouter()`; `_register_before_catchall()` (splices router routes into `chainlit.server.app` before `GET /{full_path:path}`); `auth_page()` `GET /auth` (returns `HTMLResponse(AUTH_PAGE_HTML)` — to be replaced); `login_redirect()` `GET /login` (302 → `/auth`, preserves `?next=`); `login_api()` `POST /api/auth/login` (JSON `{email,password}` → verifies via `verify_password` → `create_jwt` + `set_auth_cookie` → `{success:true}`; rate-limited); `register()` `POST /api/auth/register` (JSON `{name,email,password,confirm_password}` → `validate_password` + `get_password_hash` → insert `AppUser` → `{success:true, redirect:"/auth?tab=login&registered=1"}`); `validate_password()` (≥8 chars, ≤64, requires upper+lower+digit); `sanitize_str()` (strip + truncate 256); `_check_rate_limit/_record_failure/_clear_attempts` (in-memory, 5 attempts / 5 min window, 15 min lockout, 429); `AUTH_PAGE_HTML` (old embedded page — to be removed).
- **`database.py`** — `AsyncSessionLocal` (aiosqlite, `app.db`); `AppUser` table `app_users` with columns `id` (String PK), `name` (String, not null), `email` (String, **unique**, not null), `password_hash` (String, not null), `avatar_url` (String, nullable), `created_at` (DateTime); `verify_password(plain, hashed)` = `bcrypt.checkpw` (returns False on `ValueError`); `get_password_hash(password)` = `bcrypt.hashpw` + `bcrypt.gensalt`; `init_db()` (creates tables); `Meeting` table (unchanged, out of scope).
- **`app.py`** — `import auth` (registers the router); `@cl.password_auth_callback auth_callback(email,password)` (verifies against `AppUser` — kept for gating); conditional `@cl.oauth_callback` only when `OAUTH_GOOGLE_CLIENT_ID`+`OAUTH_GOOGLE_CLIENT_SECRET` set (they are NOT, so no oauth callback is registered); `@cl.on_chat_start/_resume/_message` (chat handlers, untouched); `@cl.data_layer` uses `SQLAlchemyDataLayer(conninfo="sqlite+aiosqlite:///app.db")`.
- **`api/index.py`** — `app = FastAPI()`; `mount_chainlit(app=app, target="app.py", path="")` → Chainlit (with the auth router) mounted at root `/`.
- **`run_app.py`** — runs `chainlit run app.py -w` (local). Loads `app.py` → `import auth` → routes on `chainlit.server.app`.
- **`vercel.json`** — catch-all `{"src":"/(.*)","dest":"api/index.py"}`; every path (including `/auth`, `/login`, `/api/auth/*`, `/dashboard`, `/public/*`) already routes to the mounted app. **No change required.**
- **`.chainlit/config.toml`** — no `[auth]` section; `custom_js="/public/custom.js"`, `custom_css="/public/theme.css"`, `logo_file_url="/public/logo.svg"` confirm `public/` is served at `/public/*`. `user_session_timeout=1296000`.
- **`public/`** — contains `auth.html` (41942 B, the design source of truth), `custom.js`, `favicon.svg`, `logo.svg`, `theme.css`. All served at `/public/*` by Chainlit's catch-all.
- **`public/auth.html` CONFIG contract** (read in full): `DEMO:true`; `endpoints.signin="/api/auth/login"` (POST `{email,password}`), `endpoints.signup="/api/auth/register"` (POST `{name,email,password}` — **no `confirm_password`**), `endpoints.reset="/api/auth/forgot-password"` (POST `{email}`); `sso: provider => "/api/auth/${provider}"` reached via `window.location.assign`; `redirect:"/dashboard"`; all fetches use `credentials:"include"`. Error handling in `submitAuth`: `const body = await res.json().catch(()=>({})); if(!res.ok) throw new Error(body.message || "We couldn't complete that…")`. Success only checks `res.ok`. Register's `showDone` shows "Welcome, {name}. … Opening your meetings…" then `window.location.assign(redirect)` after 2.4 s — i.e. **register must auto-login** (set the cookie) or the redirect to the chat would bounce back to login. SSO `startSSO` does `window.location.assign(url); await new Promise(()=>{})` (top-level navigation; a thrown error is impossible from `location.assign`, so the `catch` never fires for navigation failures).
- **`requirements.txt`** — `bcrypt>=4.0.0` (installed 5.0.0). PyJWT 2.15.1, FastAPI 0.143.0, Starlette 1.8.0 come transitively via `chainlit`. `itsdangerous` is **not** installed (and not needed).
- **`.env`** (read for key presence only; values not reproduced) — `NEXUS_API_KEY`, `NEXUS_API_BASE_URL`, `CHAINLIT_AUTH_SECRET`, `SESSION_SECRET` are set. **`OAUTH_GOOGLE_CLIENT_ID`/`OAUTH_GOOGLE_CLIENT_SECRET` are absent**; no Microsoft OAuth keys; no SMTP/email keys anywhere. Therefore SSO is unconfigured (returns 501) and forgot-password is a no-op stub (returns 200).

---

## 4. Endpoint specifications (Decision 2, 3, 5, 6)

All endpoints live on `auth_router` in `auth.py` (registered before the catch-all by the existing `_register_before_catchall()`), so they resolve identically under local `chainlit run` and Vercel's `api/index.py`. Every error response is `JSONResponse({"message": "..."}, status_code=<non-2xx>)` to satisfy the page's `body.message` contract. Success responses are 2xx (body shape is irrelevant to the page, which only checks `res.ok`).

### 4.1 `POST /api/auth/login` (modify existing `login_api`)
- **Input** (JSON): `email` (required, string), `password` (required, string).
- **Validation**: `email = sanitize_str(body.get("email","")).lower()`; `password = body.get("password","")`. Missing/empty → 422 `{"message":"Email and password are required."}`. Invalid JSON body → 400 `{"message":"Invalid request body."}`.
- **Rate limit**: reuse `_check_rate_limit(ip)`; locked out → 429 `{"message":"Too many failed attempts. Try again in 15 minutes."}`.
- **Verify**: `select(AppUser).where(AppUser.email == email)`; `if not user or not verify_password(password, user.password_hash): _record_failure(ip); return 401 {"message":"Email or password is incorrect."}`.
- **Success**: `_clear_attempts(ip)`; build `cl.User(identifier=email, metadata={"id": user.id, "name": user.name})`; `access_token = create_jwt(cl_user)`; `response = JSONResponse({"success": True}, status_code=200)`; `set_auth_cookie(request, response, access_token)`; `return response`.
- **Change vs existing**: only the error bodies move from `{"success":false,"error":...}` to `{"message":...}` (rate-limit, bad-body, missing-fields, bad-creds). Success path is unchanged.

### 4.2 `POST /api/auth/register` (modify existing `register`)
- **Input** (JSON): `name` (required), `email` (required), `password` (required). `confirm_password` is **optional** — the new page never sends it.
- **Validation** (reuse existing helpers): `name = sanitize_str(body.get("name",""))`; empty → 422 `{"message":"Please enter your full name."}`. `email = sanitize_str(body.get("email","")).lower()`; empty or no `@`/final `.` → 422 `{"message":"That email looks incomplete. Try name@example.com"}`. `pw_error = validate_password(password)`; if set → 422 `{"message": pw_error}`. `confirm_password = body.get("confirm_password")`; **if it is a non-empty string and differs from `password`** → 422 `{"message":"Passwords don't match."}` (skipped when absent, so the new page works). Invalid JSON → 400 `{"message":"Invalid request body."}`.
- **Persist**: `select(AppUser).where(AppUser.email==email)`; if exists → 409 `{"message":"That email is already registered. Try logging in."}`. Else insert `AppUser(id=str(uuid.uuid4()), name=name, email=email, password_hash=get_password_hash(password))`; commit. On DB error → 500 `{"message":"Could not create your account. Please try again."}` (logged at ERROR).
- **Success + auto-login** (NEW vs existing): build `cl.User(identifier=email, metadata={"id": new_user.id, "name": new_user.name})`; `access_token = create_jwt(cl_user)`; `response = JSONResponse({"success": True}, status_code=200)`; `set_auth_cookie(request, response, access_token)`; return. (The old `redirect:"/auth?tab=login&registered=1"` field is dropped — the page ignores it and expects to be logged in already.)
- **Rationale for auto-login**: the page's `showDone("signup", ...)` immediately shows "Opening your meetings…" and `window.location.assign("/dashboard")`. Without a cookie, that redirect hits the unauthenticated gate and bounces back to `/auth`. Auto-login is required to honor the page's UX.

### 4.3 `POST /api/auth/forgot-password` (NEW — no-op stub)
- **Input** (JSON): `email` (required, string). Invalid JSON → 400 `{"message":"Invalid request body."}`.
- **Behavior**: **always return 200** `{"message":"If an account exists for that email, a reset link has been sent."}` regardless of whether the email exists, to prevent email enumeration. Log `logger.info("Password reset requested for email=%s", email)` (the request is logged; no email is sent because no SMTP config exists).
- **Why 200 always**: the page's `submitAuth` only throws on `!res.ok`; a 200 makes it show the "Check your inbox" done-screen. Returning 404 for unknown emails would leak which emails are registered.
- **Documented stub**: real email sending is out of scope (no SMTP keys in `.env`). When SMTP config is added later (e.g. `SMTP_HOST`/`SMTP_USER`/`SMTP_PASS`/`SMTP_FROM`), replace the stub with token-generation + email send; keep the always-200, non-enumerating contract.

### 4.4 `GET /api/auth/google` and `GET /api/auth/microsoft` (NEW — SSO) — Decision 5
Both are GET because the page uses `window.location.assign` (and the rewired preflight `fetch`, see §5). The check is "are the provider's creds in the environment?". They are NOT (`.env` has no `OAUTH_GOOGLE_*`, no Microsoft keys), so today both return 501.

- **Google**: `if os.environ.get("OAUTH_GOOGLE_CLIENT_ID") and os.environ.get("OAUTH_GOOGLE_CLIENT_SECRET"):` → (future) `RedirectResponse(google_authorize_url, 302)`; else → `JSONResponse({"message":"Google sign-in is not configured yet."}, status_code=501)`.
- **Microsoft**: `if os.environ.get("OAUTH_MICROSOFT_CLIENT_ID") and os.environ.get("OAUTH_MICROSOFT_CLIENT_SECRET"):` → (future) `RedirectResponse(microsoft_authorize_url, 302)`; else → `JSONResponse({"message":"Microsoft sign-in is not configured yet."}, status_code=501)`.
- **Never fake a login**: these endpoints never set a cookie or redirect to `/dashboard` when unconfigured. A 501 is a hard refusal.
- **Why 501 + preflight (not a raw 501 after `window.location.assign`)**: the page initiates SSO with a top-level navigation; a raw 501 JSON would render as plain text in the browser (poor UX). To keep the polished inline error, `startSSO` is rewired (§5) to preflight the endpoint with `fetch(..., {redirect:"manual"})`: a configured provider returns 302 → an `opaqueredirect` response → the code performs the real `window.location.assign`; an unconfigured provider returns 501 → the code reads `body.message` and throws, so the existing click-handler `catch` shows it inline via `formError`. This honors the literal "HTTP 501 {message}" contract and keeps the UX.
- **Future real OAuth (out of scope now)**: when creds are added, implement `GET /api/auth/google/callback` (and Microsoft equivalent) that exchanges the code, looks up/creates an `AppUser`, calls `create_jwt` + `set_auth_cookie`, and `RedirectResponse("/dashboard", 302)`. Use `authlib` (add pinned dep then) or `httpx` for the token exchange. Put the OAuth `state` in the authorize URL's `state` param (not in a cookie set on the 302), because the preflight `fetch` with `redirect:"manual"` does not expose Set-Cookie to script.

### 4.5 `GET /auth` (modify existing `auth_page`)
- Serve the polished page. Use `FileResponse` reading `Path(__file__).parent / "public" / "auth.html"` (clean `/auth` URL; works locally where CWD = project root). Remove the old `AUTH_PAGE_HTML` constant and its usage.
- **Fallback (documented, for Vercel only)**: if the `__file__`-relative path does not resolve in the serverless build, change `GET /auth` to `RedirectResponse("/public/auth.html", 302)` — Chainlit's catch-already-serves-`public/` mechanism (already proven for `custom.js`/`theme.css`/`logo.svg`) serves the file. The page uses only absolute paths and inline assets, so it works at any URL.

### 4.6 `GET /login` (keep existing `login_redirect`)
- Unchanged: 302 → `/auth` (preserves `?next=`). This is the gating mechanism: Chainlit redirects unauthenticated `/` → `/login` → our `/auth`.

### 4.7 `GET /dashboard` (NEW)
- `RedirectResponse("/", 302)`. The page's `CONFIG.redirect` stays `"/dashboard"` (no edit to that line); this route lands the user on the Chainlit chat at `/` (where `mount_chainlit(path="")` serves it). With the cookie set, Chainlit authenticates and shows the chat; without it, `/` → `/login` → `/auth` (correct gating).

---

## 5. Exact `public/auth.html` edits (Decision: pixel-faithful; only 2 changes)

The page's CSS, structure, animations, copy, and form logic are **untouched**. Only:

1. **Flip demo mode** — in the `CONFIG` block, change `DEMO: true` → `DEMO: false`. (Required so the page hits real endpoints instead of simulating.)

2. **Rewire `startSSO` for the not-configured signal** — replace the body of `startSSO` so an unconfigured provider (501) surfaces inline instead of navigating into a raw JSON page:

```js
async function startSSO(provider) {
  if (CONFIG.DEMO) { await sleep(1100); return; }
  const url = CONFIG.sso(provider);
  // Preflight so a 501 "not configured" shows inline; a 302 (configured) is a real OAuth redirect.
  const res = await fetch(url, { method: "GET", redirect: "manual", credentials: "include" });
  if (res.type === "opaqueredirect") {            // provider configured → 302 to the IdP
    window.location.assign(url);
    await new Promise(() => {});
  }
  const body = await res.json().catch(() => ({})); // e.g. 501 { message: "… not configured yet." }
  throw new Error(body.message || "Sign-in is not available right now.");
}
```

The surrounding click handler (`try { await startSSO(...); showDone(...) } catch (err) { formError(friendly(err)); }`) already shows `err.message` via `formError`, so the 501 message renders inline on the polished page. No other JS, CSS, or markup changes. `CONFIG.endpoints`, `CONFIG.sso`, and `CONFIG.redirect` already match the backend routes, so **no endpoint-path edits are needed**.

> Note: this `startSSO` edit is the only behavioral JS change and is confined to one function; it does not alter the page's styles, DOM structure, or other logic. It is classified as the allowed "minor wiring."

---

## 6. Files to modify / create

| File | Action | What changes |
|---|---|---|
| `public/auth.html` | Modify | `DEMO:true`→`false`; replace `startSSO` body (§5). Nothing else. |
| `auth.py` | Modify | `GET /auth` → `FileResponse` of `public/auth.html`; remove `AUTH_PAGE_HTML`. `login_api`: error bodies → `{"message":...}`. `register`: `confirm_password` optional, errors → `{"message":...}`, add `create_jwt`+`set_auth_cookie` on success (auto-login), drop `redirect` field. Add `POST /api/auth/forgot-password` (200 stub). Add `GET /api/auth/google`, `GET /api/auth/microsoft` (501 when unconfigured). Add `GET /dashboard` → 302 `/`. |
| `app.py` | **No change** | `import auth` and `@cl.password_auth_callback` stay (gating). The conditional `@cl.oauth_callback` stays a no-op (no creds). |
| `database.py` | **No change** | Reuse `AppUser`, `get_password_hash`, `verify_password`, `AsyncSessionLocal`. |
| `api/index.py` | **No change** | `mount_chainlit(path="")` already serves the auth router at root. |
| `run_app.py` | **No change** | `chainlit run app.py -w` already loads `auth`. |
| `vercel.json` | **No change** | Catch-all already routes everything to the app. |
| `requirements.txt` | **No change** | No new deps (see §7). |
| `.env` | **No change** | `CHAINLIT_AUTH_SECRET` already set; SSO/forgot-password behavior derives from absence of creds/SMTP. **Never echo or commit secret values.** |
| `.env.example` | Optional (docs only) | May add empty placeholders `OAUTH_MICROSOFT_CLIENT_ID=`, `OAUTH_MICROSOFT_CLIENT_SECRET=`, `OAUTH_MICROSOFT_TENANT=`, `SMTP_HOST=`, `SMTP_USER=`, `SMTP_PASS=`, `SMTP_FROM=` to document future config. Non-secret placeholders only. |
| `chainlit_oauth.py` or similar | **Not created** | Real OAuth is out of scope (no creds). |

---

## 7. Dependencies (Decision: none new)

- **Session/JWT**: `create_jwt` uses **PyJWT 2.15.1** (installed transitively via `chainlit`). No need to add to `requirements.txt` (it is not "missing").
- **Password hashing**: `bcrypt` 5.0.0 (installed; `requirements.txt` pins `bcrypt>=4.0.0`). No change.
- **Web framework**: FastAPI 0.143.0 / Starlette 1.8.0 (via `chainlit`). `FileResponse`, `JSONResponse`, `RedirectResponse`, `APIRouter` are all already imported or importable from `fastapi.responses`/`fastapi`.
- **Not used / not installed**: `itsdangerous` (not installed, not needed), `authlib`/`httpx` (only needed for real OAuth token exchange — out of scope now).
- If real OAuth is added later, pin `authlib==1.3.2` (or `httpx==0.27.2`) in `requirements.txt` at that time.

---

## 8. Routing — local vs production (Decision 7)

- **Local** (`run_app.py` → `chainlit run app.py -w`): `app.py` imports `auth` → `auth_router` routes spliced into `chainlit.server.app` before the catch-all. `/auth`, `/login`, `/api/auth/*`, `/dashboard`, `/public/*`, `/` all resolve. Verify with the venv at `C:\Users\harsh\Academic\Projects\NexRecall\.venv312` (note: the venv is at the **workspace root**, not the nested project folder): `& "C:\Users\harsh\Academic\Projects\NexRecall\.venv312\Scripts\python.exe" "c:\...\NexRecall\run_app.py"` (or the `chainlit.exe` in the same `Scripts` dir).
- **Vercel** (`api/index.py`): `mount_chainlit(app=app, target="app.py", path="")` loads `app.py` → `import auth` → same routes on the mounted Chainlit app; `vercel.json`'s catch-all `/(.*)` → `api/index.py` already covers all paths. **No rewrite additions needed.** (The `FileResponse` path in `GET /auth` uses `Path(__file__).parent/"public"/"auth.html"`; if that fails on Vercel's build, use the documented `/public/auth.html` redirect fallback in §4.5.)

---

## 9. How the existing Chainlit chat stays unbroken

- The chat handlers (`on_chat_start/_resume/_message`), data layer (`SQLAlchemyDataLayer`), upload/RAG pipeline, and `@cl.password_auth_callback` are untouched.
- Gating is unchanged and correct: unauthenticated `GET /` → Chainlit redirects to `/login` → our `GET /login` 302 → `/auth` (polished page). Authenticated `GET /` (cookie present) → Chainlit reads the cookie and shows the chat directly.
- The session cookie is the *same* cookie Chainlit's own login mints (`create_jwt` + `set_auth_cookie`), so the chat needs no new auth-callback to recognize users. `@cl.password_auth_callback` is retained only to keep Chainlit's login "enabled" (which triggers the `/login` redirect for gating); our `/api/auth/login` bypasses it and mints the cookie directly, exactly as the existing code already does.
- The conditional `@cl.oauth_callback` in `app.py` stays as-is: with no `OAUTH_GOOGLE_*` it never registers, so it cannot conflict with our custom `/api/auth/google`. When real Google OAuth is wired later, decide then whether to use Chainlit's `oauth_callback` or our custom callback (out of scope now).

---

## 10. Edge cases & error handling (concrete, per operation)

| Operation | Failure condition | Recoverable? | Caller receives | Logged? (level) |
|---|---|---|---|---|
| `POST /login` bad JSON | `await request.json()` raises | Yes | 400 `{"message":"Invalid request body."}` | No |
| `POST /login` missing fields | email/password empty | Yes | 422 `{"message":"Email and password are required."}` | No |
| `POST /login` rate-limited | 5 fails / 5 min from one IP | Yes (after 15 min) | 429 `{"message":"Too many failed attempts. Try again in 15 minutes."}` | No |
| `POST /login` bad credentials | user missing or `verify_password` False | Yes | 401 `{"message":"Email or password is incorrect."}` | No (records failure) |
| `POST /login` DB error | `session.execute` raises | Fatal | 500 `{"message":"Sign-in is temporarily unavailable."}` | ERROR |
| `POST /login` success | — | — | 200 `{"success":true}` + Set-Cookie | No |
| `POST /register` duplicate email | `email` unique constraint / pre-check | Yes | 409 `{"message":"That email is already registered. Try logging in."}` | No |
| `POST /register` DB insert error | commit raises | Fatal | 500 `{"message":"Could not create your account. Please try again."}` | ERROR |
| `POST /register` success | — | — | 200 `{"success":true}` + Set-Cookie (auto-login) | INFO ("New user registered: …") |
| `POST /forgot-password` any input | always | n/a | 200 `{"message":"If an account exists…"}` | INFO (email requested) |
| `GET /api/auth/google|ms` unconfigured | creds absent | n/a | 501 `{"message":"<Provider> sign-in is not configured yet."}` | No |
| `GET /auth` file missing | `public/auth.html` absent | Fatal | 500 (FileResponse raises) | ERROR; fallback = 302 `/public/auth.html` |
| `create_jwt` no secret | `CHAINLIT_AUTH_SECRET` unset | Fatal | 500 (assert fails) | ERROR (assertion) |

All error responses are non-2xx with `{"message":...}` so the page's `submitAuth` throws and `formError`/`friendly` display them. The `friendly` helper maps a network `TypeError` (server unreachable) to "Can't reach the server. Check your connection and try again." — no backend change needed.

---

## 11. Input validation rules

| Field | Required? | Type | Limits | On failure |
|---|---|---|---|---|
| `email` (all) | Required | string | `sanitize_str` → strip + truncate 256; lowercased; must contain `@` and a `.` in the final domain part | 422/400 `{"message":...}` |
| `password` (login) | Required | string | non-empty | 422 `{"message":"Email and password are required."}` |
| `password` (register) | Required | string | `validate_password`: 8–64 chars, ≥1 upper, ≥1 lower, ≥1 digit | 422 with the specific policy message |
| `confirm_password` (register) | **Optional** | string | only checked if non-empty and ≠ password | 422 `{"message":"Passwords don't match."}` |
| `name` (register) | Required | string | `sanitize_str`; non-empty | 422 `{"message":"Please enter your full name."}` |
| `provider` (SSO) | from path | `"google"`/`"microsoft"` | route per-provider | other path → Chainlit catch-all (404) |

**Invariant ownership**: email uniqueness is owned by the **DB layer** (`AppUser.email` unique + a pre-check in the route for a friendly 409 before hitting the constraint). Password policy is owned by **`auth.py.validate_password`** (server is the source of truth; the page's client-side check is a lighter subset, so a password the page accepts but the server rejects surfaces as a clear 422 message post-submit — acceptable and secure). Credential verification is owned by **`database.verify_password`** (bcrypt). Session integrity is owned by **Chainlit** (`create_jwt`/`set_auth_cookie` + cookie validation on the chat side).

---

## 12. Testability

- **Unit (no server)**: `validate_password` (all policy branches), `sanitize_str`, `_check_rate_limit`/`_record_failure`/`_clear_attempts` (in-memory limiter), and the response shapes (status + `{"message":...}`) by calling the route functions with a mocked `Request`/`AsyncSessionLocal` (use a temp `app.db` via `init_db()`). Register's duplicate-email path and login's bad-credential path are unit-testable against a temp SQLite DB.
- **Integration (running app)**: boot with `.venv312` (`chainlit run app.py` or `run_app.py`), then with `httpx`/`requests` against `http://localhost:8000`:
  1. `GET /auth` → 200 HTML containing `DEMO`/`CONFIG` (or 302 to `/public/auth.html`).
  2. `GET /login` → 302 `/auth`.
  3. `POST /api/auth/register {name,email,password}` → 200 + `Set-Cookie` (auth cookie); second registration same email → 409 `{"message":...}`.
  4. `POST /api/auth/login {email,password}` (correct) → 200 + cookie; (wrong) → 401 `{"message":...}`; 5 wrong → 429.
  5. With the cookie, `GET /dashboard` → 302 `/`, and `GET /` returns the Chainlit chat authenticated (no redirect to `/login`).
  6. `POST /api/auth/forgot-password {email}` → 200 always (existing and non-existing email).
  7. `GET /api/auth/google` and `/api/auth/microsoft` → 501 `{"message":"… not configured yet."}`.
- **Full-flow verification** (per the task) is performed by the implementer/verification step by running the app and clicking through register → land on chat → logout → login, using the real `.venv312`.

---

## 13. Out of scope / future

- **Real OAuth (Google/Microsoft)**: not wired because no client credentials are in `.env`. Endpoints return 501. When creds are added: implement authorize-URL redirect + `/api/auth/{provider}/callback` (exchange code, upsert `AppUser`, `create_jwt`+`set_auth_cookie`, 302 `/dashboard`); add `authlib`/`httpx` pinned dep; carry OAuth `state` in the URL param.
- **Real password-reset email**: not wired (no SMTP). `forgot-password` is a documented always-200 no-op stub. When SMTP config is added, generate a single-use reset token, store it, and email a link — keeping the non-enumerating always-200 contract.
- **Chainlit built-in login page**: intentionally bypassed by the existing `/login` → `/auth` interception; not "removed" at the config level (no `[auth]` change needed). The `@cl.password_auth_callback` stays for gating.
- **Logout UI**: the custom page has no logout button; Chainlit's `POST /logout` remains. A logout button on the chat is a separate UI task.
