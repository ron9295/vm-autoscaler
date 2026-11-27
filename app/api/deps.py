"""FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.container import Container


def get_container(request: Request) -> Container:
    """The composition root built at startup (see app.main)."""
    container: Container = request.app.state.container
    return container


ContainerDep = Annotated[Container, Depends(get_container)]
