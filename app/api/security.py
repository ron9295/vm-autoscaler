"""Bearer token check for the Grafana webhook."""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status

from app.api.deps import ContainerDep


def verify_webhook_token(request: Request, container: ContainerDep) -> None:
    """Reject with 401 unless `Authorization: Bearer <WEBHOOK_TOKEN>` matches.

    Uses a constant-time comparison so the token cannot be guessed byte by byte.
    """
    header = request.headers.get("Authorization", "")
    scheme, _, presented = header.partition(" ")
    presented = presented.strip()
    expected = container.settings.webhook_token.get_secret_value()
    valid = (
        scheme.lower() == "bearer"
        and bool(presented)  # never accept an empty token, whatever the config says
        and hmac.compare_digest(presented.encode(), expected.encode())
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
