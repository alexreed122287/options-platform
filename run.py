"""Start the server: .venv/bin/python run.py"""
import json
import os
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent

if __name__ == "__main__":
    settings = json.loads((ROOT / "config" / "settings.json").read_text())
    port = int(os.environ.get("PORT", settings["server"]["default_port"]))
    # HOST=0.0.0.0 exposes the dashboard on your LAN (e.g. to open it from a
    # phone). Set DASHBOARD_TOKEN in .env when you do - the order API must
    # not be open to everyone on the network.
    host = os.environ.get("HOST", "127.0.0.1")
    uvicorn.run("api.app:app", host=host, port=port)
