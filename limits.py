"""Per-user limits, so one person can't tie up the server or use up YouTube's rate limit."""

import time
from collections import defaultdict, deque

HOUR = 3600

# start_download() result when the user already has the maximum running
BUSY = -1

# How often idle users' counters are cleared out of memory
_SWEEP_INTERVAL = 600


class UserLimits:
    def __init__(
        self,
        admin_ids: frozenset[int],
        max_active: int,
        downloads_per_hour: int,
        lookups_per_hour: int,
    ):
        self._admins = admin_ids
        self._max_active = max_active
        self._downloads_per_hour = downloads_per_hour
        self._lookups_per_hour = lookups_per_hour
        self._active: dict[int, int] = {}
        self._downloads: dict[int, deque[float]] = defaultdict(deque)
        self._lookups: dict[int, deque[float]] = defaultdict(deque)
        self._last_sweep = time.monotonic()

    def take_lookup(self, user_id: int) -> int:
        """Count a link lookup. Returns 0 if allowed, else seconds until the next one is."""
        if user_id in self._admins:
            return 0
        self._sweep()
        return self._take(self._lookups[user_id], self._lookups_per_hour)

    def start_download(self, user_id: int) -> int:
        """
        Count and start a download. Returns 0 if allowed, BUSY if the user
        already has the maximum running, else seconds until the next is allowed.
        Every allowed start must be paired with finish_download().
        """
        if user_id in self._admins:
            return 0
        self._sweep()
        if 0 < self._max_active <= self._active.get(user_id, 0):
            return BUSY
        wait = self._take(self._downloads[user_id], self._downloads_per_hour)
        if wait == 0:
            self._active[user_id] = self._active.get(user_id, 0) + 1
        return wait

    def finish_download(self, user_id: int) -> None:
        if user_id in self._admins:
            return
        if self._active.get(user_id, 0) <= 1:
            self._active.pop(user_id, None)
        else:
            self._active[user_id] -= 1

    @staticmethod
    def _take(window: deque[float], per_hour: int) -> int:
        if per_hour <= 0:
            return 0
        now = time.monotonic()
        while window and now - window[0] >= HOUR:
            window.popleft()
        if len(window) >= per_hour:
            return int(HOUR - (now - window[0])) + 1
        window.append(now)
        return 0

    def _sweep(self) -> None:
        """Drop counters of users with nothing counted in the last hour."""
        now = time.monotonic()
        if now - self._last_sweep < _SWEEP_INTERVAL:
            return
        self._last_sweep = now
        for windows in (self._downloads, self._lookups):
            for user_id in [u for u, w in windows.items() if not w or now - w[-1] >= HOUR]:
                del windows[user_id]
