"""TOML value rendering for configuration files and CLI overrides."""

import json


def toml_value(value, *, ensure_ascii: bool = False) -> str:
    """Render a JSON-compatible value as TOML.

    JSON escapes are a subset of TOML basic-string escapes, except that JSON
    leaves DEL (U+007F) raw while TOML forbids it. DEL can only occur inside a
    JSON string, so escaping it keeps any rendered value loadable.
    """
    return json.dumps(value, ensure_ascii=ensure_ascii).replace("\x7f", "\\u007f")
