"""Shared wake-up clock for background jobs.

Neon's free tier bills compute time and only sleeps after ~5 idle minutes, so jobs that poll the
database at their own rhythm keep it awake all day. Every periodic DB job waits for the scanner's
slot (:01, :16, :31, :46) instead, so the database is touched in short bursts and sleeps in between.
"""
import asyncio

import tracking
from config import SCAN_INTERVAL


async def tick():
    """Sleeps until just after the next scanner slot."""
    await asyncio.sleep(tracking.seconds_to_next_scan(SCAN_INTERVAL) + 20)


async def every(n: int):
    """Sleeps for n slots."""
    for _ in range(n):
        await tick()
