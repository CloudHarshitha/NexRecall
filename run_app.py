import sys
import nest_asyncio
# Mock nest_asyncio to prevent it from breaking anyio on Python 3.14
nest_asyncio.apply = lambda *args, **kwargs: None

from chainlit.cli import cli

if __name__ == "__main__":
    # Forward the command to chainlit, defaulting to "run app.py -w"
    sys.argv = ["chainlit", "run", "app.py", "-w"]
    cli()
