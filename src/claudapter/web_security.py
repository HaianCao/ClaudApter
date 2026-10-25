"""Authority and origin checks for the ephemeral loopback setup server."""

import secrets
from urllib.parse import urlsplit

_HOSTS = {"127.0.0.1", "localhost", "::1"}


def token_matches(value, expected):
    """Malformed input is unauthorized, never a compare_digest TypeError."""
    return isinstance(value, str) and isinstance(expected, str) and bool(expected) and value.isascii() and expected.isascii() and secrets.compare_digest(value, expected)


def local_request(headers, port: int) -> bool:
    """Reject foreign authorities/origins, including DNS-rebinding hosts."""
    hosts = headers.get_all("Host", [])
    origins = headers.get_all("Origin", [])
    if len(hosts) != 1 or len(origins) > 1:
        return False
    try:
        authority = hosts[0]
        if any(char.isspace() or ord(char) < 32 for char in authority):
            return False
        host = urlsplit("//" + authority)
        if host.hostname not in _HOSTS or host.port != port or host.username is not None or host.password is not None or host.path or host.query or host.fragment:
            return False
        if origins:
            origin = urlsplit(origins[0])
            if origin.scheme != "http" or origin.hostname != host.hostname or origin.port != port or origin.username is not None or origin.password is not None or origin.path or origin.query or origin.fragment:
                return False
        return True
    except (ValueError, TypeError):
        return False
