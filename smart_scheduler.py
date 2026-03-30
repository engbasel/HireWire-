"""
HireWire — Smart Adaptive Scheduler

Replaces the fixed 5-minute interval with intelligent scheduling:
    1. Peak hours (9 AM – 6 PM) → faster polling (4-6 min)
    2. Off-peak (night)          → slower polling (15-25 min)
    3. Randomized jitter         → avoids pattern detection
    4. Exponential backoff       → after errors or blocks

This reduces total requests by ~40% while catching projects faster
during peak freelancing hours.
"""

import random
import logging
from datetime import datetime

logger = logging.getLogger("hirewire")


# ---------------------------------------------------------------------------
# Time-of-Day Profiles
# ---------------------------------------------------------------------------
_PROFILES: dict[str, dict] = {
    "peak": {
        "hours": range(9, 19),     # 9 AM - 6 PM
        "base_minutes": 5,
        "jitter_range": (0.7, 1.3),
        "label": "🔥 Peak Hours",
    },
    "early": {
        "hours": range(6, 9),      # 6 AM - 9 AM
        "base_minutes": 10,
        "jitter_range": (0.8, 1.2),
        "label": "🌅 Early Morning",
    },
    "evening": {
        "hours": range(19, 23),    # 7 PM - 11 PM
        "base_minutes": 12,
        "jitter_range": (0.8, 1.3),
        "label": "🌆 Evening",
    },
    "night": {
        "hours": range(23, 24),    # 11 PM - 6 AM (wraps with 0-5)
        "base_minutes": 20,
        "jitter_range": (0.8, 1.5),
        "label": "🌙 Night",
    },
}


# ---------------------------------------------------------------------------
# Smart Interval Calculator
# ---------------------------------------------------------------------------
class SmartScheduler:
    """
    Adaptive scheduler that adjusts polling interval based on:
    - Time of day (peak vs off-peak)
    - Consecutive errors (exponential backoff)
    - Randomized jitter (anti-pattern-detection)
    """

    def __init__(self, min_interval: int = 3, max_interval: int = 30) -> None:
        self.min_interval = min_interval
        self.max_interval = max_interval
        self._consecutive_errors: int = 0
        self._last_interval: int = 5

    def get_interval(self) -> int:
        """
        Calculate the next polling interval in minutes.
        Accounts for time-of-day profile, error backoff, and jitter.
        """
        hour = datetime.now().hour

        # Determine time profile
        profile = self._get_profile(hour)
        base = profile["base_minutes"]
        jitter_low, jitter_high = profile["jitter_range"]

        # Apply jitter
        interval = base * random.uniform(jitter_low, jitter_high)

        # Apply error backoff (doubles interval for each consecutive error, up to 4x)
        if self._consecutive_errors > 0:
            backoff_multiplier = min(2 ** self._consecutive_errors, 4)
            interval *= backoff_multiplier
            logger.info(
                "[Scheduler] ⚠️ Error backoff active: %dx multiplier (%d consecutive errors)",
                backoff_multiplier, self._consecutive_errors,
            )

        # Clamp to bounds
        interval = max(self.min_interval, min(self.max_interval, int(interval)))

        self._last_interval = interval

        logger.info(
            "[Scheduler] %s → Next scan in %d minutes (base: %d min)",
            profile["label"], interval, base,
        )

        return interval

    def report_success(self) -> None:
        """Reset error counter after a successful cycle."""
        if self._consecutive_errors > 0:
            logger.info("[Scheduler] ✅ Recovery: resetting error backoff")
        self._consecutive_errors = 0

    def report_error(self) -> None:
        """Increment error counter for backoff calculation."""
        self._consecutive_errors += 1
        logger.warning(
            "[Scheduler] ❌ Error #%d — next interval will be extended",
            self._consecutive_errors,
        )

    @property
    def last_interval(self) -> int:
        """The most recently calculated interval."""
        return self._last_interval

    @staticmethod
    def _get_profile(hour: int) -> dict:
        """Get the scheduling profile for a given hour."""
        if hour in _PROFILES["peak"]["hours"]:
            return _PROFILES["peak"]
        if hour in _PROFILES["early"]["hours"]:
            return _PROFILES["early"]
        if hour in _PROFILES["evening"]["hours"]:
            return _PROFILES["evening"]
        # Night: 11 PM or 0-5 AM
        return _PROFILES["night"]

    def stats(self) -> dict:
        """Return scheduler statistics."""
        hour = datetime.now().hour
        profile = self._get_profile(hour)
        return {
            "current_profile": profile["label"],
            "base_interval": profile["base_minutes"],
            "last_interval": self._last_interval,
            "consecutive_errors": self._consecutive_errors,
        }
