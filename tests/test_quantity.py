import pytest

from app.domain.quantity import (
    GIB,
    QuantityError,
    format_bytes,
    grow_bytes,
    parse_bytes,
    round_up_to_gib,
)


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("512Mi", 512 * 1024**2),
        ("30Gi", 30 * GIB),
        ("64Gi", 64 * GIB),
        ("1Ti", 1024**4),
        ("1.5Gi", 3 * GIB // 2),
        ("1G", 10**9),
        ("1024", 1024),
    ],
)
def test_parse_bytes(text: str, value: int) -> None:
    assert parse_bytes(text) == value


@pytest.mark.parametrize("text", ["abc", "", "-1Gi", "1.2.3Gi", "10Xi", "500m"])
def test_parse_bytes_rejects_invalid(text: str) -> None:
    with pytest.raises(QuantityError):
        parse_bytes(text)


@pytest.mark.parametrize(
    ("value", "text"),
    [(2 * 1024**4, "2Ti"), (36 * GIB, "36Gi"), (1536 * 1024**2, "1536Mi"), (1000, "1000")],
)
def test_format_bytes(value: int, text: str) -> None:
    assert format_bytes(value) == text


def test_round_up_to_gib() -> None:
    assert round_up_to_gib(GIB + 1) == 2 * GIB
    assert round_up_to_gib(GIB) == GIB


def test_grow_bytes_rounds_up_to_whole_gib() -> None:
    assert grow_bytes(30 * GIB, 20) == 36 * GIB
    assert grow_bytes(10 * GIB, 15) == 12 * GIB  # 11.5Gi -> 12Gi
    assert grow_bytes(500 * GIB, 20) == 600 * GIB
