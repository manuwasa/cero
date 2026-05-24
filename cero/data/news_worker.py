"""
News worker.

Polls configured Twitter accounts and any other configured news sources.
Stores tweets/headlines in the `news` table for display in the dashboard.

This worker does NOT generate signals. News is informational only.
Trade blackout around scheduled events comes from calendar_worker.

TODO (Claude Code):
- Use snscrape or twscrape (free) or Twitter API (paid)
- Rate-limit, handle failures gracefully (news is optional)
- Tag each news item with source, timestamp, raw text
"""
from __future__ import annotations
