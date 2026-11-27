from decimal import Decimal

import pytest

from app.domain.quantity import (
    GIB,
    QuantityError,
    format_bytes,
    format_cpu,
    grow_bytes,
    grow_cpu,
    parse_bytes,
    parse_cpu,
    round_up_cpu,
    round_up_to_gib,
)


@pytest.mark.parametrize(
    ("text", "cores"),
    [
        ("500m", Decimal("0.5")),
        ("2", Decimal(2)),
        ("1.5", Decimal("1.5")),
        ("100m", Decimal("0.1")),
    ],
)
def test_parse_cpu(text: str, cores: Decimal) -> None:
    assert parse_cpu(text) == cores


@pytest.mark.parametrize("text", ["abc", "1Gi", "", "-1", "1.2.3"])
def test_parse_cpu_rejects_invalid(text: str) -> None:
    with pytest.raises(QuantityError):
        parse_cpu(text)


@pytest.mark.parametrize(
    ("cores", "text"),
    [(Decimal(4), "4"), (Decimal("1.5"), "1500m"), (Decimal("0.5"), "500m"), (Decimal("5.0"), "5")],
)
def test_format_cpu(cores: Decimal, text: str) -> None:
    assert format_cpu(cores) == text


def test_round_up_cpu_to_100m() -> None:
    assert round_up_cpu(Decimal("1.01")) == Decimal("1.1")
    assert round_up_cpu(Decimal("1.1")) == Decimal("1.1")
    assert round_up_cpu(Decimal("0.625")) == Decimal("0.7")


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("512Mi", 512 * 1024**2),
        ("64Gi", 64 * GIB),
        ("1Ti", 1024**4),
        ("1.5Gi", 3 * GIB // 2),
        ("1G", 10**9),
        ("1024", 1024),
    ],
)
def test_parse_bytes(text: str, value: int) -> None:
    assert parse_bytes(text) == value


def test_parse_bytes_rejects_unknown_suffix() -> None:
    with pytest.raises(QuantityError):
        parse_bytes("10Xi")


@pytest.mark.parametrize(
    ("value", "text"),
    [(2 * 1024**4, "2Ti"), (20 * GIB, "20Gi"), (1536 * 1024**2, "1536Mi"), (1000, "1000")],
)
def test_format_bytes(value: int, text: str) -> None:
    assert format_bytes(value) == text


def test_round_up_to_gib() -> None:
    assert round_up_to_gib(GIB + 1) == 2 * GIB
    assert round_up_to_gib(GIB) == GIB


def test_grow_cpu() -> None:
    assert grow_cpu(Decimal(4), 25) == Decimal(5)
    assert grow_cpu(Decimal("0.5"), 25) == Decimal("0.7")  # 0.625 -> 700m


def test_grow_bytes() -> None:
    assert grow_bytes(16 * GIB, 25) == 20 * GIB
    assert grow_bytes(10 * GIB, 15) == 12 * GIB  # 11.5Gi -> 12Gi
