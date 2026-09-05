# Copilot instructions — options-platform

- Python env: `.venv` at repo root. Entry point `run.py` starts uvicorn on port **8787**.
- `pytest.ini` sets `pythonpath=.` and `testpaths=tests`; run tests with `.venv/bin/pytest -q`.
- Broker is Tradier (`BROKER=tradier` in `.env`), not Alpaca, even though startup logs may
  print an Alpaca banner — that banner does not indicate the active broker.
- Live-trading safety gates (do not remove or bypass without explicit human confirmation
  in the same conversation turn):
  - `.env` has `LIVE_TRADING_ENABLED=false`. `routes_trading.py` raises HTTP 403 before any
    order reaches a broker if this is false, and again if `live_ack` != `"LIVE"`.
  - `engine/alerts.py` explicitly states its scan loop "ONLY notifies — this loop never
    submits orders and has no code path to the order endpoint."
  - `submit_order` has exactly one caller, behind the gate above.
- VS Code tasks: "Tests: all" (default test), "Tests: current file", "Server: run.py"
  (default build, background/dedicated terminal — safe to leave running), "Share: cloudflared
  tunnel (8787)" (background/dedicated terminal).
- Credentials live in `.env` (Tradier token, FMP key). Treat as secret; file should stay at
  mode 600.
