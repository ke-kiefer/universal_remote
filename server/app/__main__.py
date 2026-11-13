"""Start the server with host/port from .env:  uv run python -m app [--reload]"""

import sys

import uvicorn

from .config import get_settings

if __name__ == "__main__":
    s = get_settings()
    uvicorn.run("app.main:app", host=s.host, port=s.port, reload="--reload" in sys.argv)
