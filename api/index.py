from fastapi import FastAPI
from chainlit.utils import mount_chainlit

app = FastAPI()

# Mount the Chainlit app onto FastAPI so Vercel's Python runtime can serve it
mount_chainlit(app=app, target="app.py", path="")
