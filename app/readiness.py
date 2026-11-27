"""Readiness checks for /readyz.

The config is validated at startup (the pod does not start with a bad config), so
"config loaded" is always true once we are serving. Dependency reachability is optional
(`READINESS_CHECK_DEPENDENCIES=true`): any HTTP answer counts as reachable, only network
errors fail. Results are cached for 60 s so probes never hammer the backends.
"""

from __future__ import annotations

import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx

CACHE_SECONDS = 60.0
CHECK_TIMEOUT_SECONDS = 2.0


@dataclass
class ReadinessChecker:
    """Checks the configured URLs, caching the result."""

    urls: dict[str, str] = field(default_factory=dict)
    # Named async checks that must return True (e.g. "GitLab project readable").
    probes: dict[str, Callable[[], Awaitable[bool]]] = field(default_factory=dict)
    verify: ssl.SSLContext | bool = True
    transport: httpx.AsyncBaseTransport | None = None
    _cached: dict[str, bool] | None = field(default=None, init=False, repr=False)
    _cached_at: float = field(default=0.0, init=False, repr=False)

    async def check(self) -> dict[str, bool]:
        """Return {check name: ok}."""
        results = {"config": True}
        if not self.urls and not self.probes:
            return results
        now = time.monotonic()
        if self._cached is None or now - self._cached_at > CACHE_SECONDS:
            self._cached = await self._check_urls()
            self._cached_at = now
        return results | self._cached

    async def _check_urls(self) -> dict[str, bool]:
        results: dict[str, bool] = {}
        async with httpx.AsyncClient(
            timeout=CHECK_TIMEOUT_SECONDS, verify=self.verify, transport=self.transport
        ) as client:
            for name, url in self.urls.items():
                try:
                    await client.get(url)
                    results[name] = True
                except httpx.HTTPError:
                    results[name] = False
        for name, probe in self.probes.items():
            results[name] = await probe()
        return results
