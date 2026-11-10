import pytest

from claudapter.catalog import MAX_JSON_DEPTH, _decode_json


def nested(depth):
    return b"[" * depth + b"0" + b"]" * depth


def test_nesting_at_the_limit_is_accepted():
    assert _decode_json(b'{"data":' + nested(MAX_JSON_DEPTH - 1) + b"}")


@pytest.mark.parametrize("depth", [MAX_JSON_DEPTH, 10_000])
def test_deeper_nesting_is_rejected_on_every_python(depth):
    # Python 3.14 parses 10 000 levels without RecursionError.
    with pytest.raises(ValueError, match="Invalid catalog JSON"):
        _decode_json(b'{"data":' + nested(depth) + b"}")
