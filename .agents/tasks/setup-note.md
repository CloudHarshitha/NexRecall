# NexRecall Auth Integration — Setup Note

Artifact directory for the auth integration work. Confirmed paths and the
executables to use when running the app.

## Artifact directory (confirmed created)

- `c:/Users/harsh/Academic/Projects/NexRecall/NexRecall/.agents/tasks/`

## Source-of-truth auth page (confirmed exists)

- `c:/Users/harsh/Academic/Projects/NexRecall/NexRecall/public/auth.html`

## Python venv to use

> NOTE on paths: the task brief pointed the venv at
> `c:/Users/harsh/Academic/Projects/NexRecall/NexRecall/.venv312`
> (the doubled `NexRecall/NexRecall` path). That path does **not** exist.
> The actual, working venv is one directory level up, at the repo root:
> `c:/Users/harsh/Academic/Projects/NexRecall/.venv312`.
> The app source (and `auth.html`) live in the nested
> `NexRecall/NexRecall/` folder; the venv lives at the outer
> `NexRecall/` folder. Use the executables below — verified to exist and run.

Confirmed executables (use exactly these):

- Python:  `c:/Users/harsh/Academic/Projects/NexRecall/.venv312/Scripts/python.exe`  (Python 3.12.4)
- Chainlit:`c:/Users/harsh/Academic/Projects/NexRecall/.venv312/Scripts/chainlit.exe`

Example run command (from the inner app dir):

```powershell
cd c:/Users/harsh/Academic/Projects/NexRecall/NexRecall
c:/Users/harsh/Academic/Projects/NexRecall/.venv312/Scripts/chainlit.exe run ingest.py
```

## Do not use

- `c:/Users/harsh/Academic/Projects/NexRecall/.venv/` — empty/broken. Ignore.

## Verification log

- `python.exe` present in `.venv312/Scripts/` → `python --version` => `Python 3.12.4`
- `chainlit.exe` present in `.venv312/Scripts/`
- `public/auth.html` present in the inner app dir
- `.agents/tasks/` directory created
