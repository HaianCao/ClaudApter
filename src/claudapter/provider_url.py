"""Provider base URL rules and child-process proxy settings.

Shared verbatim by ClaudApter and CodexApter, with no Protoverta dependency,
so setup can validate an address even when the gateway component is broken.
"""

from urllib.parse import urlsplit

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_ENDPOINT_SUFFIXES = ("/responses", "/messages", "/chat/completions")


def normalize_base_url(value: object) -> str:
    """Return the provider API prefix without a trailing slash.

    Raises ``ValueError`` with a message for the user when the address is not
    an HTTPS URL (loopback HTTP is allowed) or carries credentials, a query,
    a fragment or an endpoint suffix.
    """
    if not isinstance(value, str) or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Provider base URL must be text without whitespace or control characters.")
    try:
        parsed = urlsplit(value)
        port = parsed.port  # Force validation of a malformed port number.
    except ValueError as exc:
        raise ValueError("Provider base URL is invalid.") from exc
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Provider base URL must be an HTTPS URL without credentials, query, or fragment.")
    if port is not None and port < 1:
        raise ValueError("Provider base URL port must be between 1 and 65535.")
    # urlsplit lowercases the scheme and host.
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in _LOOPBACK_HOSTS):
        raise ValueError("Provider base URL must use HTTPS (HTTP is allowed only for localhost/loopback).")
    if parsed.path.rstrip("/").endswith(_ENDPOINT_SUFFIXES):
        raise ValueError("Enter the base URL, for example https://provider.example/v1, without the endpoint suffix.")
    return value.rstrip("/")


def bypass_loopback_proxy(env: dict[str, str]) -> None:
    """Keep the CLI's calls to the local gateway or retry relay away from a configured proxy."""
    entries = [
        value.strip() for key in ("NO_PROXY", "no_proxy") for value in env.get(key, "").split(",") if value.strip()
    ]
    entries.extend(("127.0.0.1", "localhost", "::1"))
    merged = ",".join({value.casefold(): value for value in entries}.values())
    env.update(NO_PROXY=merged, no_proxy=merged)
