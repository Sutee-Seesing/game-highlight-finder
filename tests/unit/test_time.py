from __future__ import annotations

import pytest

from game_highlight_finder.domain.time import format_duration, seconds_to_ms
from game_highlight_finder.errors import ValidationError


@pytest.mark.parametrize(
    ("seconds", "milliseconds"),
    [("0", 0), ("1.234", 1234), ("1.2345", 1235), (2, 2000)],
)
def test_seconds_to_integer_milliseconds(seconds: str | int, milliseconds: int) -> None:
    assert seconds_to_ms(seconds) == milliseconds


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "nope"])
def test_invalid_seconds_are_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        seconds_to_ms(value)


def test_duration_format() -> None:
    assert format_duration(3_723_004) == "01:02:03.004"
