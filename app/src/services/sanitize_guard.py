"""Per-user rate/concurrency guards for file sanitization requests."""
import asyncio
import time
from collections import defaultdict, deque

from fastapi import HTTPException

from src.core import config

_sanitize_user_timestamps: dict[int, deque[float]] = defaultdict(deque)
_sanitize_user_active: dict[int, int] = defaultdict(int)
_sanitize_guard_lock = asyncio.Lock()


async def _acquire_sanitize_slot(user_id: int) -> None:
    now = time.time()
    async with _sanitize_guard_lock:
        timestamps = _sanitize_user_timestamps[user_id]
        while timestamps and now - timestamps[0] > 60:
            timestamps.popleft()

        if _sanitize_user_active[user_id] >= config.SANITIZE_MAX_CONCURRENT_PER_USER:
            raise HTTPException(status_code=429, detail="Too many concurrent sanitize requests")

        if len(timestamps) >= config.SANITIZE_MAX_PER_MINUTE:
            raise HTTPException(status_code=429, detail="Sanitize rate limit exceeded")

        _sanitize_user_active[user_id] += 1


async def _release_sanitize_slot(user_id: int) -> None:
    async with _sanitize_guard_lock:
        _sanitize_user_active[user_id] = max(0, _sanitize_user_active[user_id] - 1)
