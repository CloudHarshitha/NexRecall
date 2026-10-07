import sys
import nest_asyncio
# Mock nest_asyncio to prevent it from breaking anyio on Python 3.14
nest_asyncio.apply = lambda *args, **kwargs: None

from chainlit.cli import cli

if __name__ == "__main__":
    sys.argv = ["chainlit", "run", "app.py"]
    cli()
