"""Start the server: .venv/bin/python run.py"""
import json
import os
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent

if __name__ == "__main__":
    settings = json.loads((ROOT / "config" / "settings.json").read_text())
    port = int(os.environ.get("PORT", settings["server"]["default_port"]))
    uvicorn.run("api.app:app", host="127.0.0.1", port=port)
