import uuid
from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.routing import APIRouter
from chainlit.server import app as chainlit_app
from database import AsyncSessionLocal, AppUser, get_password_hash
from sqlalchemy.future import select

auth_router = APIRouter()

SIGNUP_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Sign Up - NexRecall</title>
    <style>
        body {
            background-color: #121212;
            color: #ffffff;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
        }
        .container {
            background-color: #1e1e1e;
            padding: 40px;
            border-radius: 12px;
            box-shadow: 0 4px 6px rgba(0, 0, 0, 0.3);
            width: 100%;
            max-width: 400px;
        }
        h2 {
            text-align: center;
            margin-bottom: 24px;
        }
        form {
            display: flex;
            flex-direction: column;
        }
        input {
            background-color: #2c2c2c;
            border: 1px solid #3c3c3c;
            color: white;
            padding: 12px;
            margin-bottom: 16px;
            border-radius: 8px;
            font-size: 16px;
            outline: none;
        }
        input:focus {
            border-color: #d81b60;
        }
        button {
            background-color: #d81b60;
            color: white;
            border: none;
            padding: 14px;
            border-radius: 8px;
            font-size: 16px;
            font-weight: bold;
            cursor: pointer;
            transition: background-color 0.2s;
        }
        button:hover {
            background-color: #ad144b;
        }
        .error {
            color: #ff5252;
            margin-bottom: 16px;
            text-align: center;
        }
        .link {
            text-align: center;
            margin-top: 16px;
        }
        .link a {
            color: #d81b60;
            text-decoration: none;
        }
        .link a:hover {
            text-decoration: underline;
        }
    </style>
</head>
<body>
    <div class="container">
        <h2>Create Account</h2>
        {error_html}
        <form action="/signup" method="post">
            <input type="text" name="name" placeholder="Full Name" required>
            <input type="email" name="email" placeholder="Email Address" required>
            <input type="password" name="password" placeholder="Password (min 8 chars)" required minlength="8">
            <input type="password" name="confirm_password" placeholder="Confirm Password" required minlength="8">
            <button type="submit">Sign Up</button>
        </form>
        <div class="link">
            Already have an account? <a href="/login">Sign in</a>
        </div>
    </div>
</body>
</html>
"""

@auth_router.get("/signup", response_class=HTMLResponse)
async def signup_get(request: Request):
    return SIGNUP_HTML.format(error_html="")

@auth_router.post("/signup", response_class=HTMLResponse)
async def signup_post(request: Request):
    form = await request.form()
    name = form.get("name")
    email = form.get("email")
    password = form.get("password")
    confirm_password = form.get("confirm_password")

    error = None
    if not name or not email or not password or not confirm_password:
        error = "All fields are required."
    elif len(password) < 8:
        error = "Password must be at least 8 characters."
    elif password != confirm_password:
        error = "Passwords do not match."

    if error:
        return SIGNUP_HTML.format(error_html=f'<div class="error">{error}</div>')

    async with AsyncSessionLocal() as session:
        stmt = select(AppUser).where(AppUser.email == email)
        result = await session.execute(stmt)
        if result.scalar_one_or_none():
            error = "Email already registered."
            return SIGNUP_HTML.format(error_html=f'<div class="error">{error}</div>')

        new_user = AppUser(
            id=str(uuid.uuid4()),
            name=name,
            email=email,
            password_hash=get_password_hash(password)
        )
        session.add(new_user)
        await session.commit()

    return RedirectResponse(url="/login", status_code=303)

chainlit_app.include_router(auth_router)
