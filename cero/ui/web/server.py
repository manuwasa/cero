"""
FastAPI web dashboard.

Serves the static dashboard files + a JSON/WebSocket API for live data.

Endpoints to implement:

  GET  /                      → static/index.html
  GET  /static/*              → static files
  GET  /api/account           → equity, dd, position count
  GET  /api/positions         → list of open positions
  GET  /api/readiness/{sym}   → current scoring for a symbol
  GET  /api/candles/{sym}/{tf} → recent candles for charting
  GET  /api/news              → recent news items
  GET  /api/calendar          → upcoming events
  GET  /api/pnl               → performance stats
  POST /api/trip              → fire the kill switch
  POST /api/reset             → un-trip
  POST /api/mode              → change mode
  WS   /ws/live               → pushes price updates, signal updates, fills

TODO (Claude Code):
- Mount static files at /static
- All endpoints share the same DB/state as the rest of the app
- WebSocket pushes events from the same internal pubsub the brain uses
- Keep it 127.0.0.1 only; no auth needed for localhost
- If exposing beyond localhost: add token auth + HTTPS
"""
from __future__ import annotations
