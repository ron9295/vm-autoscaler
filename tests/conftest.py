"""Shared test helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.config import AppConfig, load_app_config

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


@pytest.fixture
def app_config() -> AppConfig:
    return load_app_config(ROOT / "config.example.yaml")
