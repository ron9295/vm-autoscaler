"""Small helpers shared by the HTTP adapters."""

from __future__ import annotations

import ssl
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.domain.errors import GitOpsUnavailableError


def build_verify(
    ca_bundle_path: Path | None, insecure_skip_verify: bool = False
) -> ssl.SSLContext | bool:
    """TLS verification setting for httpx: internal CA bundle, system CAs, or (discouraged) off."""
    if insecure_skip_verify:
        return False
    if ca_bundle_path is not None:
        return ssl.create_default_context(cafile=str(ca_bundle_path))
    return True


def truncate(text: str, limit: int = 500) -> str:
    """Cut a response body to `limit` characters for error messages."""
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


# Errors where the request provably never left this process: safe to retry even a write.
NOT_SENT_ERRORS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
)


class _ServerError(Exception):
    """Internal marker for a 5xx answer (retryable for GET only)."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"HTTP {response.status_code}")


async def send_with_retries(
    http: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    max_retries: int,
    retry_wait: float,
    scrub: Callable[[str], str] = str,
    **kwargs: Any,
) -> tuple[httpx.Response, int]:
    """Send a request with the GitOps retry rules. Returns (response, attempts).

    * GET: retried on connection errors, timeouts and 5xx (exponential backoff + jitter).
    * Anything else: retried ONLY if the request never left this process, so a write that
      the server may have received is never sent twice.
    Raises GitOpsUnavailableError when giving up. `scrub` removes secrets from messages.
    """
    retry_on = (httpx.TransportError, _ServerError) if method == "GET" else NOT_SENT_ERRORS
    attempts = 0
    retrying = AsyncRetrying(
        stop=stop_after_attempt(max_retries + 1),
        wait=wait_exponential_jitter(initial=retry_wait, max=retry_wait * 20, jitter=retry_wait),
        retry=retry_if_exception_type(retry_on),
        reraise=True,
    )
    try:
        async for attempt in retrying:
            with attempt:
                attempts += 1
                response = await http.request(method, path, **kwargs)
                if response.status_code >= 500:
                    raise _ServerError(response)
                return response, attempts
    except _ServerError as exc:
        body = scrub(truncate(exc.response.text))
        raise GitOpsUnavailableError(
            f"{method} {path}: HTTP {exc.response.status_code} after {attempts} attempt(s): {body}"
        ) from exc
    except httpx.TransportError as exc:  # includes all timeouts
        raise GitOpsUnavailableError(
            f"{method} {path}: {type(exc).__name__} after {attempts} attempt(s)"
        ) from exc
    raise GitOpsUnavailableError(f"{method} {path}: no attempt made")  # pragma: no cover
