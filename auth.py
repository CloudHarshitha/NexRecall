"""
auth.py - Custom authentication routes for NexRecall.

Adds two endpoints to the Chainlit FastAPI app:
  GET  /auth          → serves the combined login/sign-up HTML page
  POST /api/auth/register → creates a new user, then delegates to
                            Chainlit's own POST /login so we get the
                            same JWT cookie without duplicating logic.

The login form POSTs directly to Chainlit's own POST /login endpoint
(OAuth2PasswordRequestForm: username + password fields), which calls
our @cl.password_auth_callback defined in app.py.
"""

import os
import re
import uuid
import time
import logging
from collections import defaultdict

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from chainlit.server import app as chainlit_app
from database import AsyncSessionLocal, AppUser, get_password_hash, verify_password, init_db
from sqlalchemy.future import select

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Simple in-memory rate limiter (resets on process restart — acceptable for
# Render free-tier single-instance deployments).
# Tracks failed login attempts per IP.  After MAX_ATTEMPTS within WINDOW_SECS
# the IP is locked out for LOCKOUT_SECS.
# ---------------------------------------------------------------------------
MAX_ATTEMPTS = 5          # failed attempts before lockout
WINDOW_SECS = 300         # sliding window (5 minutes)
LOCKOUT_SECS = 900        # lockout duration (15 minutes)

_attempts: dict[str, list[float]] = defaultdict(list)

def _check_rate_limit(ip: str) -> bool:
    """Return True if the IP is allowed, False if locked out."""
    now = time.monotonic()
    # Prune timestamps outside the window
    _attempts[ip] = [t for t in _attempts[ip] if now - t < WINDOW_SECS]
    return len(_attempts[ip]) < MAX_ATTEMPTS

def _record_failure(ip: str) -> None:
    _attempts[ip].append(time.monotonic())

def _clear_attempts(ip: str) -> None:
    _attempts.pop(ip, None)

# ---------------------------------------------------------------------------
# Password policy (mirrors the front-end checklist)
# ---------------------------------------------------------------------------
PW_MIN = 8
PW_MAX = 64
_RE_UPPER  = re.compile(r"[A-Z]")
_RE_LOWER  = re.compile(r"[a-z]")
_RE_DIGIT  = re.compile(r"\d")

def validate_password(pw: str) -> str | None:
    """Return an error message or None if the password is valid."""
    if len(pw) < PW_MIN:
        return f"Password must be at least {PW_MIN} characters."
    if len(pw) > PW_MAX:
        return f"Password must be at most {PW_MAX} characters."
    if not _RE_UPPER.search(pw):
        return "Password must contain at least one uppercase letter."
    if not _RE_LOWER.search(pw):
        return "Password must contain at least one lowercase letter."
    if not _RE_DIGIT.search(pw):
        return "Password must contain at least one number."
    return None

def sanitize_str(value: str, max_len: int = 256) -> str:
    """Strip whitespace and truncate to prevent oversized inputs."""
    return value.strip()[:max_len]

# ---------------------------------------------------------------------------
# Auth router
# ---------------------------------------------------------------------------
auth_router = APIRouter()


@auth_router.get("/auth", response_class=HTMLResponse)
async def auth_page(request: Request):
    """Serve the combined login / sign-up page (public/auth.html)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public", "auth.html")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("Auth page not found.", status_code=500)


@auth_router.get("/login")
async def login_redirect(request: Request):
    """
    Server-side redirect: /login → /auth.

    Chainlit's React SPA redirects unauthenticated users to /login on the
    client side. By intercepting /login here (before Chainlit's catch-all
    GET /{full_path:path} handler), we send a real HTTP 302 so the browser
    lands directly on /auth without ever loading the React SPA, eliminating
    the blank-page flash caused by the double client-side redirect.

    We preserve any ?next= query param so deep links still work after login.
    """
    from fastapi.responses import RedirectResponse as _Redirect
    next_url = request.query_params.get("next", "")
    dest = "/auth" + (f"?next={next_url}" if next_url else "")
    return _Redirect(url=dest, status_code=302)


@auth_router.post("/api/auth/login")
async def login_api(request: Request):
    """
    Custom login endpoint that bypasses Chainlit's /login route (which has
    an anyio/threadpool incompatibility with nest_asyncio).

    Accepts JSON: { email, password }
    On success: sets the Chainlit auth cookie and returns { success: true }.
    On failure: returns { success: false, message: "..." } (non-2xx).
    """
    from chainlit.server import create_jwt, set_auth_cookie

    await init_db()

    ip = request.client.host if request.client else "unknown"

    # Rate-limit check
    if not _check_rate_limit(ip):
        return JSONResponse(
            {"success": False, "message": "Too many failed attempts. Try again in 15 minutes."},
            status_code=429,
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid request body."}, status_code=400)

    email    = sanitize_str(body.get("email", "")).lower()
    password = body.get("password", "")

    if not email or not password:
        return JSONResponse({"success": False, "message": "Email and password are required."}, status_code=422)

    # Verify credentials
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(AppUser).where(AppUser.email == email))
        user = result.scalar_one_or_none()

    if not user or not verify_password(password, user.password_hash):
        _record_failure(ip)
        return JSONResponse(
            {"success": False, "message": "Email or password is incorrect."},
            status_code=401,
        )

    _clear_attempts(ip)

    # Build a Chainlit User and mint the JWT
    import chainlit as cl
    cl_user = cl.User(
        identifier=email,
        metadata={"id": user.id, "name": user.name},
    )
    access_token = create_jwt(cl_user)

    # Return success with the cookie set
    response = JSONResponse({"success": True, "message": "Signed in."})
    set_auth_cookie(request, response, access_token)
    return response


@auth_router.post("/api/auth/register")
async def register(request: Request):
    """
    Create a new user account.

    Accepts JSON: { name, email, password, confirm_password? }
    Returns JSON: { success, message?, redirect? }
    """
    await init_db()

    # --- Parse body ---
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid request body."}, status_code=400)

    name             = sanitize_str(body.get("name", ""))
    email            = sanitize_str(body.get("email", "")).lower()
    password         = body.get("password", "")
    confirm_password = body.get("confirm_password", password)  # optional for the new UI

    # --- Server-side validation ---
    if not name:
        return JSONResponse({"success": False, "message": "Please enter your full name."}, status_code=422)
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        return JSONResponse({"success": False, "message": "That email looks incomplete. Try name@example.com"}, status_code=422)
    pw_error = validate_password(password)
    if pw_error:
        return JSONResponse({"success": False, "message": pw_error}, status_code=422)
    if password != confirm_password:
        return JSONResponse({"success": False, "message": "Passwords don't match."}, status_code=422)

    # --- Persist user ---
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(AppUser).where(AppUser.email == email))
        if result.scalar_one_or_none():
            return JSONResponse(
                {"success": False, "message": "That email is already registered. Try logging in."},
                status_code=409,
            )
        new_user = AppUser(
            id=str(uuid.uuid4()),
            name=name,
            email=email,
            password_hash=get_password_hash(password),
        )
        session.add(new_user)
        await session.commit()

    logger.info("New user registered: %s", email)
    return JSONResponse({"success": True, "message": "Account created.", "redirect": "/"})


@auth_router.post("/api/auth/forgot-password")
async def forgot_password(request: Request):
    """
    Password reset request.

    Accepts JSON: { email }
    Always returns 200 with a generic message to avoid email enumeration.
    Email sending is not wired up (no SMTP config) — this is a safe no-op stub.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    email = sanitize_str(body.get("email", "")).lower()
    if email:
        logger.info("Password reset requested for: %s", email)
    return JSONResponse(
        {"success": True, "message": "If an account exists, a reset link is on its way."},
        status_code=200,
    )


def _sso_not_configured_html(provider_name: str) -> str:
    html = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in · NexRecall</title>
<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;font-family:system-ui,"Segoe UI",Roboto,sans-serif;background:#06030F;color:#F4EEFF;text-align:center;padding:24px}
.box{max-width:440px;padding:40px 32px;border:1px solid rgba(255,255,255,.12);border-radius:22px;background:linear-gradient(180deg,rgba(40,20,82,.5),rgba(14,6,32,.62))}
h1{font-size:24px;margin:0 0 12px;letter-spacing:-.02em}
p{color:#A99BCB;line-height:1.55;margin:0 0 24px;font-size:15.5px}
a{display:inline-block;padding:11px 20px;border-radius:999px;background:#6EF0C4;color:#06281D;text-decoration:none;font-weight:650;font-size:15px}
</style></head>
<body><div class="box">
<h1>__PROVIDER__ sign-in isn't configured yet</h1>
<p>This provider hasn't been set up on the server. Use email and password to continue, or ask your admin to enable __PROVIDER__ sign-in.</p>
<a href="/auth">Back to sign in</a>
</div></body></html>"""
    return html.replace("__PROVIDER__", provider_name)


@auth_router.get("/api/auth/google")
async def sso_google(request: Request):
    """Google SSO entry. Redirects to OAuth when configured, else a friendly notice."""
    if os.environ.get("OAUTH_GOOGLE_CLIENT_ID") and os.environ.get("OAUTH_GOOGLE_CLIENT_SECRET"):
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/login/oauth/google", status_code=302)
    return HTMLResponse(_sso_not_configured_html("Google"), status_code=501)


@auth_router.get("/api/auth/microsoft")
async def sso_microsoft(request: Request):
    """Microsoft SSO entry. Not configured — returns a friendly notice."""
    return HTMLResponse(_sso_not_configured_html("Microsoft"), status_code=501)


# ---------------------------------------------------------------------------
# Route registration – must happen BEFORE Chainlit's catch-all GET
# /{full_path:path} route, otherwise that catch-all swallows /auth and
# returns the SPA shell instead of our HTML.
#
# Strategy: build the APIRoute objects from auth_router and splice them
# into Chainlit's included router just before the catch-all entry.
# ---------------------------------------------------------------------------
from fastapi.routing import APIRoute as _APIRoute

def _register_before_catchall() -> None:
    """Insert auth_router routes into Chainlit's router before the catch-all.

    Works with both old Chainlit (routes in an _IncludedRouter wrapper) and
    new Starlette ≥0.47 (routes flattened directly onto app.routes).
    """
    from chainlit.server import app as _cl_app
    from fastapi.routing import APIRoute as _APIRoute

    our_routes = [r for r in auth_router.routes if isinstance(r, _APIRoute)]

    # Try flat structure first (starlette >= 0.47)
    target_list = None
    for r in _cl_app.routes:
        if isinstance(r, _APIRoute) and getattr(r, "path", "") == "/{full_path:path}":
            target_list = _cl_app.routes
            break

    # Fallback: old _IncludedRouter structure
    if target_list is None:
        for r in _cl_app.routes:
            orig = getattr(r, "original_router", None)
            if orig is not None:
                for sub in orig.routes:
                    if isinstance(sub, _APIRoute) and getattr(sub, "path", "") == "/{full_path:path}":
                        target_list = orig.routes
                        break
            if target_list is not None:
                break

    if target_list is None:
        # Last resort: just append to app.routes
        target_list = _cl_app.routes

    catchall_idx = next(
        (i for i, r in enumerate(target_list)
         if isinstance(r, _APIRoute) and getattr(r, "path", "") == "/{full_path:path}"),
        len(target_list),
    )

    for offset, route in enumerate(our_routes):
        target_list.insert(catchall_idx + offset, route)

    # Bust any route-matching cache on the app router
    for attr in ("_route_cache", "_cached_routes"):
        if hasattr(_cl_app.router, attr):
            try:
                delattr(_cl_app.router, attr)
            except Exception:
                pass

_register_before_catchall()


# ---------------------------------------------------------------------------
# Combined login / sign-up HTML page
# ---------------------------------------------------------------------------
AUTH_PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta name="description" content="Sign in or create your NexRecall account." />
  <title>NexRecall – Sign in</title>

  <!-- ============================================================
       CSS VARIABLES – retheme by editing this block only
       ============================================================ -->
  <style>
    /* ─── Tokens ─────────────────────────────────────────────── */
    :root {
      --color-bg:          #ffffff;
      --color-surface:     #ffffff;
      --color-surface-2:   #f9f9f9;
      --color-border:      #e5e5e5;
      --color-border-focus:#8fbc8b;

      --color-accent:      #8fbc8b;
      --color-accent-dark: #7dae79;
      --color-accent-light:#5f8f5b;

      --color-text:        #0d0d0d;
      --color-text-muted:  #5d5d5d;
      --color-text-subtle: #5d5d5d;

      --color-error:       #f87171;
      --color-error-bg:    #fff1f1;
      --color-error-border:#f87171;

      --color-success:     #8fbc8b;
      --color-success-bg:  #e9f3e8;

      --color-warn-dim:    #e9f3e8;

      /* Password strength */
      --color-pw-weak:     #f87171;
      --color-pw-okay:     #fbbf24;
      --color-pw-strong:   #8fbc8b;

      --radius-sm:  8px;
      --radius-md:  8px;
      --radius-lg:  12px;
      --radius-full: 999px;

      --space-xs:  4px;
      --space-sm:  8px;
      --space-md:  16px;
      --space-lg:  24px;
      --space-xl:  32px;
      --space-2xl: 48px;

      --font-base: Inter, system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
      --font-size-sm:  13px;
      --font-size-md:  15px;
      --font-size-lg:  16px;
      --font-size-xl:  22px;
      --font-size-2xl: 28px;

      --transition: 150ms ease;
      --shadow-card: none;
    }

    @media (prefers-color-scheme: dark) {
      :root {
        --color-bg:          #212121;
        --color-surface:     #212121;
        --color-surface-2:   #171717;
        --color-border:      #424242;
        --color-border-focus:#8fbc8b;

        --color-accent:      #8fbc8b;
        --color-accent-dark: #7dae79;
        --color-accent-light:#8fbc8b;

        --color-text:        #ececec;
        --color-text-muted:  #b4b4b4;
        --color-text-subtle: #b4b4b4;
        
        --color-error-bg:    #2a1616;
        --color-error-border:#7f2d2d;
        --color-success-bg:  #2a3a2a;
      }
    }

    /* ─── Reset / base ───────────────────────────────────────── */
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    html { scroll-behavior: smooth; }

    body {
      background: var(--color-bg);
      color: var(--color-text);
      font-family: var(--font-base);
      font-size: var(--font-size-md);
      min-height: 100dvh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: var(--space-md);
    }

    /* ─── Reduced-motion override ────────────────────────────── */
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { transition: none !important; animation: none !important; }
    }

    /* ─── Card ───────────────────────────────────────────────── */
    .card {
      background: var(--color-surface);
      border: 1px solid var(--color-border);
      border-radius: var(--radius-lg);
      box-shadow: var(--shadow-card);
      width: 100%;
      max-width: 440px;
      padding: var(--space-xl) var(--space-xl) var(--space-lg);
    }

    /* ─── Brand mark ─────────────────────────────────────────── */
    .brand {
      display: flex;
      align-items: center;
      gap: var(--space-sm);
      margin-bottom: var(--space-xl);
    }
    .brand-icon {
      width: 36px; height: 36px;
      background: var(--color-accent);
      border-radius: var(--radius-md);
      display: grid; place-items: center;
      flex-shrink: 0;
    }
    .brand-icon svg { width: 20px; height: 20px; }
    .brand-name {
      font-size: var(--font-size-xl);
      font-weight: 700;
      letter-spacing: -0.5px;
      color: var(--color-text);
    }

    /* ─── Tab strip ──────────────────────────────────────────── */
    .tab-list {
      display: grid;
      grid-template-columns: 1fr 1fr;
      background: var(--color-surface-2);
      border-radius: var(--radius-md);
      padding: 3px;
      margin-bottom: var(--space-lg);
      border: 1px solid var(--color-border);
    }
    .tab-btn {
      background: none;
      border: none;
      color: var(--color-text-muted);
      font-size: var(--font-size-md);
      font-weight: 500;
      padding: 9px var(--space-md);
      border-radius: calc(var(--radius-md) - 2px);
      cursor: pointer;
      transition: background var(--transition), color var(--transition);
      min-height: 44px;
    }
    .tab-btn:hover:not([aria-selected="true"]) {
      color: var(--color-text);
    }
    .tab-btn[aria-selected="true"] {
      background: var(--color-accent);
      color: #fff;
    }
    .tab-btn:focus-visible {
      outline: 2px solid var(--color-border-focus);
      outline-offset: 2px;
    }

    /* ─── Tab panels ─────────────────────────────────────────── */
    .tab-panel { display: none; }
    .tab-panel.active { display: block; }

    /* ─── Headings / subtext ─────────────────────────────────── */
    .form-heading {
      font-size: var(--font-size-2xl);
      font-weight: 700;
      letter-spacing: -0.5px;
      line-height: 1.2;
      margin-bottom: var(--space-xs);
    }
    .form-subtext {
      color: var(--color-text-muted);
      font-size: var(--font-size-sm);
      margin-bottom: var(--space-lg);
      line-height: 1.5;
    }

    /* ─── Field group ────────────────────────────────────────── */
    .field {
      margin-bottom: var(--space-md);
    }
    .field label {
      display: block;
      font-size: var(--font-size-sm);
      font-weight: 500;
      color: var(--color-text-muted);
      margin-bottom: var(--space-xs);
    }
    .field-row {
      position: relative;
      display: flex;
      align-items: center;
    }
    .field-row input {
      width: 100%;
      background: var(--color-surface-2);
      border: 1.5px solid var(--color-border);
      border-radius: var(--radius-md);
      color: var(--color-text);
      font-size: var(--font-size-md);
      padding: 11px var(--space-md);
      transition: border-color var(--transition), box-shadow var(--transition);
      /* Allow password-with-button to not overlap text */
    }
    .field-row input[type="password"],
    .field-row input.has-toggle {
      padding-right: 44px;
    }
    .field-row input::placeholder { color: var(--color-text-subtle); }
    .field-row input:focus {
      outline: none;
      border-color: var(--color-border-focus);
      box-shadow: 0 0 0 3px #7c6af722;
    }
    .field-row input[aria-invalid="true"] {
      border-color: var(--color-error);
      box-shadow: 0 0 0 3px #f8717122;
    }
    .field-row input[aria-invalid="false"]:not(:placeholder-shown) {
      border-color: var(--color-success);
    }

    /* Show / hide password toggle */
    .toggle-pw {
      position: absolute;
      right: 0;
      top: 0;
      height: 100%;
      width: 44px;
      display: grid;
      place-items: center;
      background: none;
      border: none;
      color: var(--color-text-muted);
      cursor: pointer;
      border-radius: 0 var(--radius-md) var(--radius-md) 0;
      transition: color var(--transition);
      /* min touch target */
      min-width: 44px;
    }
    .toggle-pw:hover { color: var(--color-text); }
    .toggle-pw:focus-visible {
      outline: 2px solid var(--color-border-focus);
      outline-offset: -2px;
    }
    .toggle-pw svg { width: 18px; height: 18px; pointer-events: none; }

    /* Error / hint text below field */
    .field-error {
      font-size: var(--font-size-sm);
      color: var(--color-error);
      margin-top: var(--space-xs);
      display: none;
      align-items: center;
      gap: 4px;
    }
    .field-error.visible { display: flex; }
    .field-error svg { width: 13px; height: 13px; flex-shrink: 0; }

    /* ─── Password strength meter ────────────────────────────── */
    .pw-strength {
      margin-top: var(--space-sm);
    }
    .pw-strength-bar {
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 4px;
      margin-bottom: 6px;
    }
    .pw-bar-seg {
      height: 4px;
      border-radius: var(--radius-full);
      background: var(--color-border);
      transition: background 300ms ease;
    }
    .pw-bar-seg.lit-weak   { background: var(--color-pw-weak); }
    .pw-bar-seg.lit-okay   { background: var(--color-pw-okay); }
    .pw-bar-seg.lit-strong { background: var(--color-pw-strong); }

    .pw-label {
      font-size: var(--font-size-sm);
      color: var(--color-text-muted);
      margin-bottom: 6px;
    }
    .pw-label span { font-weight: 600; }
    .pw-label .lbl-weak   { color: var(--color-pw-weak); }
    .pw-label .lbl-okay   { color: var(--color-pw-okay); }
    .pw-label .lbl-strong { color: var(--color-pw-strong); }

    .pw-checklist {
      list-style: none;
      display: flex;
      flex-direction: column;
      gap: 3px;
    }
    .pw-checklist li {
      font-size: var(--font-size-sm);
      color: var(--color-text-muted);
      display: flex;
      align-items: center;
      gap: 6px;
      transition: color var(--transition);
    }
    .pw-checklist li svg { width: 14px; height: 14px; flex-shrink: 0; }
    .pw-checklist li.met {
      color: var(--color-success);
    }

    /* ─── Checkbox rows ──────────────────────────────────────── */
    .check-row {
      display: flex;
      align-items: flex-start;
      gap: var(--space-sm);
      margin-bottom: var(--space-md);
    }
    .check-row input[type="checkbox"] {
      width: 18px; height: 18px;
      flex-shrink: 0;
      margin-top: 1px;
      accent-color: var(--color-accent);
      cursor: pointer;
      /* Ensure 44px touch target by increasing clickable area via wrapper */
    }
    .check-row label {
      font-size: var(--font-size-sm);
      color: var(--color-text-muted);
      cursor: pointer;
      line-height: 1.5;
    }
    .check-row a {
      color: var(--color-accent-light);
      text-decoration: none;
    }
    .check-row a:hover { text-decoration: underline; }
    .check-row a:focus-visible {
      outline: 2px solid var(--color-border-focus);
      outline-offset: 2px;
      border-radius: 2px;
    }

    /* Remember me + forgot password row */
    .remember-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: var(--space-md);
    }
    .remember-row .check-row { margin-bottom: 0; }
    .forgot-link {
      font-size: var(--font-size-sm);
      color: var(--color-accent-light);
      text-decoration: none;
      white-space: nowrap;
    }
    .forgot-link:hover { text-decoration: underline; }
    .forgot-link:focus-visible {
      outline: 2px solid var(--color-border-focus);
      outline-offset: 2px;
      border-radius: 2px;
    }

    /* ─── Submit button ──────────────────────────────────────── */
    .btn-primary {
      width: 100%;
      background: var(--color-accent);
      color: #fff;
      border: none;
      border-radius: var(--radius-md);
      font-size: var(--font-size-md);
      font-weight: 600;
      padding: 13px var(--space-lg);
      cursor: pointer;
      transition: background var(--transition), opacity var(--transition),
                  transform 80ms ease;
      min-height: 40px;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: var(--space-sm);
      margin-top: var(--space-sm);
    }
    .btn-primary:hover:not(:disabled) { background: var(--color-accent-dark); }
    .btn-primary:active:not(:disabled) { transform: scale(0.98); }
    .btn-primary:disabled {
      opacity: 0.55;
      cursor: not-allowed;
    }
    .btn-primary:focus-visible {
      outline: 2px solid var(--color-border-focus);
      outline-offset: 3px;
    }
    /* Spinner inside button */
    .btn-spinner {
      display: none;
      width: 18px; height: 18px;
      border: 2px solid #ffffff60;
      border-top-color: #fff;
      border-radius: 50%;
      animation: spin 0.7s linear infinite;
    }
    .btn-primary.loading .btn-spinner { display: block; }
    .btn-primary.loading .btn-label   { display: none; }
    @keyframes spin { to { transform: rotate(360deg); } }

    /* ─── Banner (server errors / success) ───────────────────── */
    .banner {
      border-radius: var(--radius-md);
      padding: var(--space-sm) var(--space-md);
      font-size: var(--font-size-sm);
      display: none;
      align-items: flex-start;
      gap: var(--space-sm);
      margin-bottom: var(--space-md);
      line-height: 1.5;
    }
    .banner.visible { display: flex; }
    .banner svg { width: 16px; height: 16px; flex-shrink: 0; margin-top: 1px; }
    .banner.error {
      background: var(--color-error-bg);
      border: 1px solid var(--color-error-border);
      color: var(--color-error);
    }
    .banner.success {
      background: var(--color-success-bg);
      border: 1px solid #15543e;
      color: var(--color-success);
    }

    /* ─── Footer switch ──────────────────────────────────────── */
    .switch-link {
      text-align: center;
      font-size: var(--font-size-sm);
      color: var(--color-text-muted);
      margin-top: var(--space-lg);
    }
    .switch-link button {
      background: none;
      border: none;
      color: var(--color-accent-light);
      font-size: inherit;
      cursor: pointer;
      text-decoration: underline;
      padding: 0;
    }
    .switch-link button:focus-visible {
      outline: 2px solid var(--color-border-focus);
      outline-offset: 2px;
      border-radius: 2px;
    }

    /* ─── Divider ────────────────────────────────────────────── */
    .divider {
      border: none;
      border-top: 1px solid var(--color-border);
      margin: var(--space-lg) 0;
    }
  </style>
</head>

<body>
  <main class="card" id="main-card">
    <!-- Brand -->
    <div class="brand" aria-hidden="true">
      <div class="brand-icon">
        <img src="/public/logo.svg" alt="NexRecall Logo" width="36" height="36" />
      </div>
      <span class="brand-name"><span style="color:var(--color-text);font-weight:500;">Nex</span><span style="color:var(--color-accent-light);font-weight:500;">Recall</span></span>
    </div>

    <!-- Tab navigation -->
    <div class="tab-list" role="tablist" aria-label="Authentication">
      <button
        class="tab-btn"
        id="tab-login"
        role="tab"
        aria-selected="true"
        aria-controls="panel-login"
        tabindex="0"
      >Log in</button>
      <button
        class="tab-btn"
        id="tab-signup"
        role="tab"
        aria-selected="false"
        aria-controls="panel-signup"
        tabindex="-1"
      >Sign up</button>
    </div>

    <!-- ══════════════════════════════════════════════════════════
         LOGIN PANEL
         ══════════════════════════════════════════════════════════ -->
    <div
      id="panel-login"
      role="tabpanel"
      aria-labelledby="tab-login"
      class="tab-panel active"
    >
      <h1 class="form-heading">Welcome back.</h1>
      <p class="form-subtext">Your meetings are right where you left them.</p>

      <!-- Server error / success banner -->
      <div
        id="login-banner"
        class="banner error"
        role="alert"
        aria-live="assertive"
        aria-atomic="true"
        tabindex="-1"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/>
          <line x1="12" y1="16" x2="12.01" y2="16"/>
        </svg>
        <span id="login-banner-text"></span>
      </div>

      <form id="login-form" novalidate autocomplete="on">
        <!-- Email -->
        <div class="field">
          <label for="login-email">Email address</label>
          <div class="field-row">
            <input
              id="login-email"
              name="username"
              type="email"
              autocomplete="email"
              inputmode="email"
              placeholder="you@example.com"
              required
              aria-required="true"
              aria-describedby="login-email-err"
            />
          </div>
          <div id="login-email-err" class="field-error" role="alert">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
                 stroke-linecap="round" aria-hidden="true">
              <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/>
              <line x1="12" y1="16" x2="12.01" y2="16"/>
            </svg>
            <span></span>
          </div>
        </div>

        <!-- Password -->
        <div class="field">
          <label for="login-password">Password</label>
          <div class="field-row">
            <input
              id="login-password"
              name="password"
              type="password"
              autocomplete="current-password"
              placeholder="Your password"
              required
              aria-required="true"
              aria-describedby="login-password-err"
              class="has-toggle"
            />
            <button
              type="button"
              class="toggle-pw"
              aria-pressed="false"
              aria-label="Show password"
              data-for="login-password"
            >
              <!-- Eye icon (show) -->
              <svg class="icon-show" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                   stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
                <circle cx="12" cy="12" r="3"/>
              </svg>
              <!-- Eye-off icon (hide) — hidden by default -->
              <svg class="icon-hide" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                   stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
                   aria-hidden="true" style="display:none">
                <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8
                         a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4
                         c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07
                         a3 3 0 1 1-4.24-4.24"/>
                <line x1="1" y1="1" x2="23" y2="23"/>
              </svg>
            </button>
          </div>
          <div id="login-password-err" class="field-error" role="alert">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
                 stroke-linecap="round" aria-hidden="true">
              <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/>
              <line x1="12" y1="16" x2="12.01" y2="16"/>
            </svg>
            <span></span>
          </div>
        </div>

        <!-- Remember me + forgot -->
        <div class="remember-row">
          <div class="check-row">
            <input type="checkbox" id="login-remember" name="remember" />
            <label for="login-remember">Remember me</label>
          </div>
          <a href="#" class="forgot-link" id="forgot-link">Forgot password?</a>
        </div>

        <button type="submit" class="btn-primary" id="login-submit">
          <div class="btn-spinner" aria-hidden="true"></div>
          <span class="btn-label">Log in</span>
        </button>
      </form>

      <div class="switch-link">
        Don't have an account?
        <button type="button" onclick="switchTab('signup')">Sign up free</button>
      </div>
    </div><!-- /panel-login -->

    <!-- ══════════════════════════════════════════════════════════
         SIGN-UP PANEL
         ══════════════════════════════════════════════════════════ -->
    <div
      id="panel-signup"
      role="tabpanel"
      aria-labelledby="tab-signup"
      class="tab-panel"
    >
      <h1 class="form-heading">Create your account.</h1>
      <p class="form-subtext">It takes under a minute.</p>

      <!-- Server error banner -->
      <div
        id="signup-banner"
        class="banner error"
        role="alert"
        aria-live="assertive"
        aria-atomic="true"
        tabindex="-1"
      >
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
             stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/>
          <line x1="12" y1="16" x2="12.01" y2="16"/>
        </svg>
        <span id="signup-banner-text"></span>
      </div>

      <form id="signup-form" novalidate autocomplete="on">
        <!-- Full name -->
        <div class="field">
          <label for="signup-name">Full name</label>
          <div class="field-row">
            <input
              id="signup-name"
              name="name"
              type="text"
              autocomplete="name"
              placeholder="Alex Smith"
              required
              aria-required="true"
              aria-describedby="signup-name-err"
            />
          </div>
          <div id="signup-name-err" class="field-error" role="alert">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
                 stroke-linecap="round" aria-hidden="true">
              <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/>
              <line x1="12" y1="16" x2="12.01" y2="16"/>
            </svg>
            <span></span>
          </div>
        </div>

        <!-- Email -->
        <div class="field">
          <label for="signup-email">Email address</label>
          <div class="field-row">
            <input
              id="signup-email"
              name="email"
              type="email"
              autocomplete="email"
              inputmode="email"
              placeholder="you@example.com"
              required
              aria-required="true"
              aria-describedby="signup-email-err"
            />
          </div>
          <div id="signup-email-err" class="field-error" role="alert">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
                 stroke-linecap="round" aria-hidden="true">
              <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/>
              <line x1="12" y1="16" x2="12.01" y2="16"/>
            </svg>
            <span></span>
          </div>
        </div>

        <!-- Password -->
        <div class="field">
          <label for="signup-password">Password</label>
          <div class="field-row">
            <input
              id="signup-password"
              name="password"
              type="password"
              autocomplete="new-password"
              placeholder="Create a password"
              required
              aria-required="true"
              aria-describedby="signup-password-err signup-pw-strength"
              class="has-toggle"
            />
            <button
              type="button"
              class="toggle-pw"
              aria-pressed="false"
              aria-label="Show password"
              data-for="signup-password"
            >
              <svg class="icon-show" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                   stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
                <circle cx="12" cy="12" r="3"/>
              </svg>
              <svg class="icon-hide" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                   stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
                   aria-hidden="true" style="display:none">
                <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8
                         a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4
                         c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07
                         a3 3 0 1 1-4.24-4.24"/>
                <line x1="1" y1="1" x2="23" y2="23"/>
              </svg>
            </button>
          </div>
          <div id="signup-password-err" class="field-error" role="alert">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
                 stroke-linecap="round" aria-hidden="true">
              <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/>
              <line x1="12" y1="16" x2="12.01" y2="16"/>
            </svg>
            <span></span>
          </div>
          <!-- Strength meter -->
          <div
            id="signup-pw-strength"
            class="pw-strength"
            aria-live="polite"
            aria-atomic="true"
            style="display:none"
          >
            <div class="pw-strength-bar" aria-hidden="true">
              <div class="pw-bar-seg" id="seg-1"></div>
              <div class="pw-bar-seg" id="seg-2"></div>
              <div class="pw-bar-seg" id="seg-3"></div>
            </div>
            <p class="pw-label">
              Strength: <span id="pw-strength-label">–</span>
            </p>
            <ul class="pw-checklist" aria-label="Password requirements">
              <li id="chk-len">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
                     stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <circle cx="12" cy="12" r="10"/>
                  <polyline points="9 12 11 14 15 10" class="check-mark" style="display:none"/>
                  <line x1="8" y1="12" x2="16" y2="12" class="dash-mark"/>
                </svg>
                At least 8 characters
              </li>
              <li id="chk-upper">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
                     stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <circle cx="12" cy="12" r="10"/>
                  <polyline points="9 12 11 14 15 10" class="check-mark" style="display:none"/>
                  <line x1="8" y1="12" x2="16" y2="12" class="dash-mark"/>
                </svg>
                One uppercase letter (A–Z)
              </li>
              <li id="chk-lower">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
                     stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <circle cx="12" cy="12" r="10"/>
                  <polyline points="9 12 11 14 15 10" class="check-mark" style="display:none"/>
                  <line x1="8" y1="12" x2="16" y2="12" class="dash-mark"/>
                </svg>
                One lowercase letter (a–z)
              </li>
              <li id="chk-digit">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
                     stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                  <circle cx="12" cy="12" r="10"/>
                  <polyline points="9 12 11 14 15 10" class="check-mark" style="display:none"/>
                  <line x1="8" y1="12" x2="16" y2="12" class="dash-mark"/>
                </svg>
                One number (0–9)
              </li>
            </ul>
          </div>
        </div>

        <!-- Confirm password -->
        <div class="field">
          <label for="signup-confirm">Confirm password</label>
          <div class="field-row">
            <input
              id="signup-confirm"
              name="confirm_password"
              type="password"
              autocomplete="new-password"
              placeholder="Repeat your password"
              required
              aria-required="true"
              aria-describedby="signup-confirm-err"
              class="has-toggle"
            />
            <button
              type="button"
              class="toggle-pw"
              aria-pressed="false"
              aria-label="Show password"
              data-for="signup-confirm"
            >
              <svg class="icon-show" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                   stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
                <circle cx="12" cy="12" r="3"/>
              </svg>
              <svg class="icon-hide" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                   stroke-width="2" stroke-linecap="round" stroke-linejoin="round"
                   aria-hidden="true" style="display:none">
                <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8
                         a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4
                         c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07
                         a3 3 0 1 1-4.24-4.24"/>
                <line x1="1" y1="1" x2="23" y2="23"/>
              </svg>
            </button>
          </div>
          <div id="signup-confirm-err" class="field-error" role="alert">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
                 stroke-linecap="round" aria-hidden="true">
              <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/>
              <line x1="12" y1="16" x2="12.01" y2="16"/>
            </svg>
            <span></span>
          </div>
        </div>

        <!-- Terms checkbox -->
        <div class="check-row">
          <input
            type="checkbox"
            id="signup-terms"
            name="terms"
            required
            aria-required="true"
            aria-describedby="signup-terms-err"
          />
          <label for="signup-terms">
            I agree to the
            <a href="#" onclick="return false;">Terms</a> and
            <a href="#" onclick="return false;">Privacy Policy</a>
          </label>
        </div>
        <div id="signup-terms-err" class="field-error" role="alert" style="margin-top:-8px;margin-bottom:12px">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"
               stroke-linecap="round" aria-hidden="true">
            <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/>
            <line x1="12" y1="16" x2="12.01" y2="16"/>
          </svg>
          <span>Please accept the Terms and Privacy Policy to continue.</span>
        </div>

        <button type="submit" class="btn-primary" id="signup-submit">
          <div class="btn-spinner" aria-hidden="true"></div>
          <span class="btn-label">Create account</span>
        </button>
      </form>

      <div class="switch-link">
        Already have an account?
        <button type="button" onclick="switchTab('login')">Log in</button>
      </div>
    </div><!-- /panel-signup -->
  </main><!-- /card -->

  <!-- ============================================================
       JAVASCRIPT – tabs, validation, submission
       ============================================================ -->
  <script>
  (function () {
    'use strict';

    // ── Helpers ──────────────────────────────────────────────────────
    function qs(sel, ctx) { return (ctx || document).querySelector(sel); }
    function setError(errEl, msg) {
      const span = errEl.querySelector('span');
      if (span) span.textContent = msg;
      errEl.classList.add('visible');
      // Mark the associated input as invalid
      const inputId = errEl.id.replace('-err', '').replace('signup-', 'signup-').replace('login-', 'login-');
      const input = document.getElementById(inputId);
      if (input) { input.setAttribute('aria-invalid', 'true'); }
    }
    function clearError(errEl) {
      errEl.classList.remove('visible');
      const inputId = errEl.id.replace('-err', '').replace('signup-', 'signup-').replace('login-', 'login-');
      const input = document.getElementById(inputId);
      if (input) { input.setAttribute('aria-invalid', 'false'); }
    }
    function showBanner(bannerEl, textEl, msg, type) {
      bannerEl.className = 'banner ' + type + ' visible';
      textEl.textContent = msg;
      bannerEl.focus();
    }
    function hideBanner(bannerEl) { bannerEl.classList.remove('visible'); }

    // ── Tab switching ─────────────────────────────────────────────────
    const tabs = { login: qs('#tab-login'), signup: qs('#tab-signup') };
    const panels = { login: qs('#panel-login'), signup: qs('#panel-signup') };
    // Preserve shared email across tabs
    let sharedEmail = '';

    window.switchTab = function (name) {
      const other = name === 'login' ? 'signup' : 'login';
      // Save current email
      const fromEmail = qs('#' + other + '-email');
      const toEmail   = qs('#' + name  + '-email');
      if (fromEmail) sharedEmail = fromEmail.value;

      // Update tabs
      tabs[other].setAttribute('aria-selected', 'false');
      tabs[other].setAttribute('tabindex', '-1');
      tabs[name].setAttribute('aria-selected', 'true');
      tabs[name].setAttribute('tabindex', '0');

      // Update panels
      panels[other].classList.remove('active');
      panels[name].classList.add('active');

      // Restore email
      if (toEmail && sharedEmail) toEmail.value = sharedEmail;

      // Focus heading
      const heading = qs('.form-heading', panels[name]);
      if (heading) { heading.setAttribute('tabindex', '-1'); heading.focus(); }

      // Update URL without reload
      const url = new URL(window.location.href);
      url.searchParams.set('tab', name);
      history.replaceState(null, '', url.toString());
    };

    // Keyboard arrow-key navigation for tabs
    [tabs.login, tabs.signup].forEach(function (btn, i, arr) {
      btn.addEventListener('keydown', function (e) {
        let next = null;
        if (e.key === 'ArrowRight' || e.key === 'ArrowDown') { next = arr[(i + 1) % arr.length]; }
        if (e.key === 'ArrowLeft'  || e.key === 'ArrowUp')   { next = arr[(i - 1 + arr.length) % arr.length]; }
        if (next) { e.preventDefault(); next.click(); next.focus(); }
      });
      btn.addEventListener('click', function () {
        switchTab(btn.getAttribute('aria-controls').replace('panel-', ''));
      });
    });

    // ── Show/hide password toggles ────────────────────────────────────
    document.querySelectorAll('.toggle-pw').forEach(function (btn) {
      btn.addEventListener('click', function () {
        const input = document.getElementById(btn.dataset.for);
        if (!input) return;
        const isVisible = input.type === 'text';
        input.type = isVisible ? 'password' : 'text';
        btn.setAttribute('aria-pressed', String(!isVisible));
        btn.setAttribute('aria-label', isVisible ? 'Show password' : 'Hide password');
        btn.querySelector('.icon-show').style.display = isVisible ? '' : 'none';
        btn.querySelector('.icon-hide').style.display = isVisible ? 'none' : '';
      });
    });

    // ── Password strength ─────────────────────────────────────────────
    const pwInput   = qs('#signup-password');
    const strengthEl = qs('#signup-pw-strength');
    const strengthLabel = qs('#pw-strength-label');
    const segs = [qs('#seg-1'), qs('#seg-2'), qs('#seg-3')];
    const chks = {
      len:   qs('#chk-len'),
      upper: qs('#chk-upper'),
      lower: qs('#chk-lower'),
      digit: qs('#chk-digit'),
    };

    function checkRule(li, pass) {
      li.classList.toggle('met', pass);
      const mark = li.querySelector('.check-mark');
      const dash = li.querySelector('.dash-mark');
      if (mark) mark.style.display = pass ? '' : 'none';
      if (dash) dash.style.display = pass ? 'none' : '';
    }

    function scorePassword(pw) {
      const rules = {
        len:   pw.length >= 8,
        upper: /[A-Z]/.test(pw),
        lower: /[a-z]/.test(pw),
        digit: /\d/.test(pw),
      };
      const count = Object.values(rules).filter(Boolean).length;
      return { rules, count };
    }

    pwInput.addEventListener('input', function () {
      const pw = pwInput.value;
      if (!pw) {
        strengthEl.style.display = 'none';
        return;
      }
      strengthEl.style.display = '';
      const { rules, count } = scorePassword(pw);

      checkRule(chks.len,   rules.len);
      checkRule(chks.upper, rules.upper);
      checkRule(chks.lower, rules.lower);
      checkRule(chks.digit, rules.digit);

      // Extra point for length >= 12
      const bonus = pw.length >= 12 ? 1 : 0;
      const total = count + bonus;

      let level = 'weak', labelText = 'Weak', labelClass = 'lbl-weak';
      if (total >= 5) { level = 'strong'; labelText = 'Strong'; labelClass = 'lbl-strong'; }
      else if (total >= 3) { level = 'okay';  labelText = 'Okay';   labelClass = 'lbl-okay'; }

      segs.forEach(function (s) { s.className = 'pw-bar-seg'; });
      if (level === 'weak')   { segs[0].classList.add('lit-weak'); }
      if (level === 'okay')   { segs[0].classList.add('lit-okay'); segs[1].classList.add('lit-okay'); }
      if (level === 'strong') { segs.forEach(function(s){ s.classList.add('lit-strong'); }); }

      strengthLabel.innerHTML = '<span class="' + labelClass + '">' + labelText + '</span>';
    });

    // ── Validation helpers ────────────────────────────────────────────
    function validateEmail(val) {
      val = val.trim();
      if (!val) return 'Please enter your email address.';
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(val))
        return 'That email looks incomplete. Try name@example.com';
      return null;
    }
    function validatePassword(val) {
      if (!val) return 'Please enter a password.';
      if (val.length < 8) return 'Password must be at least 8 characters.';
      if (val.length > 64) return 'Password is too long (max 64 characters).';
      if (!/[A-Z]/.test(val)) return 'Add at least one uppercase letter.';
      if (!/[a-z]/.test(val)) return 'Add at least one lowercase letter.';
      if (!/\d/.test(val))    return 'Add at least one number.';
      return null;
    }

    // ── Blur + live validation ─────────────────────────────────────────
    // Track which fields have been touched
    const touched = {};

    function attachValidation(inputEl, errEl, validator) {
      inputEl.addEventListener('blur', function () {
        touched[inputEl.id] = true;
        const err = validator(inputEl.value);
        if (err) setError(errEl, err); else clearError(errEl);
      });
      inputEl.addEventListener('input', function () {
        if (!touched[inputEl.id]) return;
        const err = validator(inputEl.value);
        if (err) setError(errEl, err); else clearError(errEl);
      });
    }

    // Login fields
    attachValidation(
      qs('#login-email'), qs('#login-email-err'), validateEmail
    );
    attachValidation(
      qs('#login-password'), qs('#login-password-err'),
      function (v) { return v ? null : 'Please enter your password.'; }
    );

    // Signup fields
    attachValidation(
      qs('#signup-name'), qs('#signup-name-err'),
      function (v) { return v.trim() ? null : 'Please enter your full name.'; }
    );
    attachValidation(qs('#signup-email'), qs('#signup-email-err'), validateEmail);
    attachValidation(qs('#signup-password'), qs('#signup-password-err'), validatePassword);

    const confirmEl  = qs('#signup-confirm');
    const confirmErr = qs('#signup-confirm-err');
    confirmEl.addEventListener('blur', function () {
      touched[confirmEl.id] = true;
      validateConfirm();
    });
    confirmEl.addEventListener('input', function () {
      if (!touched[confirmEl.id]) return;
      validateConfirm();
    });
    pwInput.addEventListener('input', function () {
      if (!touched[confirmEl.id]) return;
      validateConfirm();
    });
    function validateConfirm() {
      if (confirmEl.value && confirmEl.value !== pwInput.value)
        setError(confirmErr, "Passwords don't match yet.");
      else clearError(confirmErr);
    }

    // ── LOGIN FORM SUBMISSION ─────────────────────────────────────────
    const loginForm   = qs('#login-form');
    const loginBtn    = qs('#login-submit');
    const loginBanner = qs('#login-banner');
    const loginBannerText = qs('#login-banner-text');

    loginForm.addEventListener('submit', async function (e) {
      e.preventDefault();
      hideBanner(loginBanner);

      const email = qs('#login-email').value.trim();
      const password = qs('#login-password').value;

      // Touch all fields
      touched['login-email']    = true;
      touched['login-password'] = true;

      let valid = true;
      const emailErr = validateEmail(email);
      if (emailErr) { setError(qs('#login-email-err'), emailErr); valid = false; }
      else clearError(qs('#login-email-err'));
      if (!password) { setError(qs('#login-password-err'), 'Please enter your password.'); valid = false; }
      else clearError(qs('#login-password-err'));
      if (!valid) return;

      // Loading state
      loginBtn.disabled = true;
      loginBtn.classList.add('loading');

      try {
        // POST to our own /api/auth/login endpoint (JSON).
        // Chainlit's built-in /login has an anyio event-loop incompatibility
        // when nest_asyncio is active, so we handle auth ourselves.
        const res = await fetch('/api/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ email, password }),
          credentials: 'include',
        });

        const data = await res.json().catch(() => ({}));

        if (res.ok && data.success) {
          // Cookie is set by the server; redirect to the chat app.
          window.location.href = '/';
        } else {
          const detail = data.error || 'Email or password is incorrect.';
          showBanner(loginBanner, loginBannerText, detail, 'error');
        }
      } catch (err) {
        showBanner(loginBanner, loginBannerText,
          'Network error. Please check your connection and try again.', 'error');
      } finally {
        loginBtn.disabled = false;
        loginBtn.classList.remove('loading');
      }
    });

    // ── SIGNUP FORM SUBMISSION ────────────────────────────────────────
    const signupForm   = qs('#signup-form');
    const signupBtn    = qs('#signup-submit');
    const signupBanner = qs('#signup-banner');
    const signupBannerText = qs('#signup-banner-text');

    signupForm.addEventListener('submit', async function (e) {
      e.preventDefault();
      hideBanner(signupBanner);

      const name    = qs('#signup-name').value.trim();
      const email   = qs('#signup-email').value.trim();
      const pw      = qs('#signup-password').value;
      const confirm = qs('#signup-confirm').value;
      const terms   = qs('#signup-terms').checked;

      // Touch all fields
      ['signup-name','signup-email','signup-password','signup-confirm'].forEach(function(id){
        touched[id] = true;
      });

      let valid = true;
      function check(inputId, errId, validator) {
        const err = validator(document.getElementById(inputId).value);
        if (err) { setError(document.getElementById(errId), err); valid = false; }
        else clearError(document.getElementById(errId));
      }

      check('signup-name',     'signup-name-err',
            function (v) { return v.trim() ? null : 'Please enter your full name.'; });
      check('signup-email',    'signup-email-err', validateEmail);
      check('signup-password', 'signup-password-err', validatePassword);

      if (confirm !== pw) {
        setError(confirmErr, "Passwords don't match yet."); valid = false;
      } else clearError(confirmErr);

      const termsErr = qs('#signup-terms-err');
      if (!terms) { termsErr.classList.add('visible'); valid = false; }
      else         { termsErr.classList.remove('visible'); }

      if (!valid) return;

      // Loading state
      signupBtn.disabled = true;
      signupBtn.classList.add('loading');

      try {
        const res = await fetch('/api/auth/register', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, email, password: pw, confirm_password: confirm }),
          credentials: 'include',
        });

        const data = await res.json().catch(function () { return {}; });

        if (res.ok && data.success) {
          // Auto-switch to login tab with success hint and pre-filled email
          sharedEmail = email;
          switchTab('login');
          // Show success banner on login panel
          showBanner(loginBanner, loginBannerText,
            'Account created! Please log in.', 'success');
          loginBanner.className = 'banner success visible';
        } else {
          showBanner(signupBanner, signupBannerText,
            data.error || 'Something went wrong. Please try again.', 'error');
        }
      } catch (err) {
        showBanner(signupBanner, signupBannerText,
          'Network error. Please check your connection and try again.', 'error');
      } finally {
        signupBtn.disabled = false;
        signupBtn.classList.remove('loading');
      }
    });

    // ── Forgot password placeholder ───────────────────────────────────
    qs('#forgot-link').addEventListener('click', function (e) {
      e.preventDefault();
      showBanner(loginBanner, loginBannerText,
        'Password reset is not yet available. Please contact support.', 'error');
    });

    // ── Initialise from URL params ────────────────────────────────────
    (function init() {
      const params = new URLSearchParams(window.location.search);
      const tab = params.get('tab');
      if (tab === 'signup') switchTab('signup');

      // ?registered=1 → show success on login tab
      if (params.get('registered') === '1') {
        showBanner(loginBanner, loginBannerText,
          'Account created! Please log in.', 'success');
        loginBanner.className = 'banner success visible';
      }
    }());
  }());
  </script>
</body>
</html>
"""
