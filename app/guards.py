"""Small production guards: lead detection and per-client rate limiting."""
import re
import time
from typing import Dict, Optional, Tuple

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"\+?\(?\d[\d\s().-]{5,20}\d")


def extract_lead(text: str) -> Optional[dict]:
    """Pull an email and/or phone number out of free text typed into the chat."""
    lead = {}
    email = EMAIL.search(text)
    if email:
        lead["email"] = email.group(0).lower()
    for match in PHONE.finditer(text):
        digits = re.sub(r"[^\d+]", "", match.group(0))
        if 7 <= len(digits.lstrip("+")) <= 15:  # plausible phone length, not an order number
            lead["phone"] = digits
            break
    return lead or None


class RateLimiter:
    """Token bucket per key (client IP). Cheap protection for a public endpoint
    that spends LLM tokens on every call."""

    def __init__(self, rate_per_min: float = 20, burst: int = 5) -> None:
        self.rate = rate_per_min / 60.0
        self.burst = burst
        self._buckets: Dict[str, Tuple[float, float]] = {}

    def allow(self, key: str, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        tokens, last = self._buckets.get(key, (float(self.burst), now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        if tokens < 1:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1, now)
        if len(self._buckets) > 10000:  # keep memory bounded
            self._buckets.clear()
        return True
