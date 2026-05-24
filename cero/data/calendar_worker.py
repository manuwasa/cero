"""
Calendar worker.

Fetches the economic calendar (e.g., from ForexFactory) hourly.
Stores events in `calendar_events` with impact level and scheduled time.
The brain uses this to enforce news blackouts.

TODO (Claude Code):
- Scrape ForexFactory weekly calendar (free, simple HTML)
- Filter by configured impact levels (low/medium/high)
- Store next 7 days of events
- Refresh every hour
"""
from __future__ import annotations
