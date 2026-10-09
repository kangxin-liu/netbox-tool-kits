# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Environment configuration readers with fail-fast validation.

Every setting this app has comes from the environment (Requirement 2.1). A missing required
variable stops startup naming the variable, rather than leaving the app half-working with a
default nobody chose (Requirement 2.2).

Nothing here logs or returns a value that could contain the API token: `require` raises with the
variable *name* only.
"""

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger("importer.config")

# `http://` is permitted to these hosts only, and the exception is scoped by host rather than by
# a boolean anyone can flip (Requirement 2.3a/2.3b).
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def require(environ: dict[str, str], name: str) -> str:
    """Return a non-empty environment value, or refuse to start."""
    value = (environ.get(name) or "").strip()
    if not value:
        raise ImproperlyConfigured(
            f"{name} is not set. This app reads its configuration from the environment; "
            f"copy .env.example to .env and set {name} before starting."
        )
    return value


def optional(environ: dict[str, str], name: str, default: str = "") -> str:
    value = environ.get(name)
    return default if value is None else value.strip()


def flag(environ: dict[str, str], name: str, default: bool) -> bool:
    raw = optional(environ, name)
    if raw == "":
        return default
    lowered = raw.lower()
    if lowered in TRUE_VALUES:
        return True
    if lowered in FALSE_VALUES:
        return False
    raise ImproperlyConfigured(
        f"{name} must be a boolean (one of {sorted(TRUE_VALUES | FALSE_VALUES)}), got {raw!r}"
    )


def integer(environ: dict[str, str], name: str, default: int, *, minimum: int = 1) -> int:
    raw = optional(environ, name)
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ImproperlyConfigured(f"{name} must be an integer, got {raw!r}") from None
    if value < minimum:
        raise ImproperlyConfigured(f"{name} must be >= {minimum}, got {value}")
    return value


def csv_list(environ: dict[str, str], name: str, default: tuple[str, ...] = ()) -> list[str]:
    raw = optional(environ, name)
    if raw == "":
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def version_tuple(
    environ: dict[str, str], name: str, default: tuple[int, ...]
) -> tuple[int, ...]:
    """Read a dotted version like ``4.6`` into an int tuple, or fall back to ``default``.

    Lets an operator widen the supported NetBox range for a specific environment (e.g. a staging
    instance pinned to an older release) without editing code. Non-numeric or empty parts are
    refused at startup rather than silently coerced, so a typo cannot quietly disable the check.
    """
    raw = optional(environ, name)
    if raw == "":
        return default
    parts: list[int] = []
    for chunk in raw.split("."):
        chunk = chunk.strip()
        if not chunk.isdigit():
            raise ImproperlyConfigured(
                f"{name} must be a dotted version made of integers, e.g. 4.6 — got {raw!r}"
            )
        parts.append(int(chunk))
    if not parts:
        raise ImproperlyConfigured(f"{name} must be a dotted version, e.g. 4.6 — got {raw!r}")
    return tuple(parts)


@dataclass(frozen=True)
class NetBoxTarget:
    """Where this app writes, and whether the loopback development exception is in play."""

    url: str
    host: str
    scheme: str
    loopback_dev: bool

    @property
    def display(self) -> str:
        return f"{self.scheme}://{self.host}"


def netbox_target(url: str) -> NetBoxTarget:
    """Validate NETBOX_URL's scheme and host (Requirements 2.3a, 2.3b, 14.5).

    HTTPS is the only transport to a remote NetBox. A loopback host may use `http://` so that a
    local netbox-docker instance is usable in development; every other plaintext target is refused
    at startup so the exception cannot be stretched to a shared or production instance.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        raise ImproperlyConfigured(
            f"NETBOX_URL must be an absolute http(s) URL, e.g. https://netbox.example.internal/ "
            f"— got {url!r}"
        )

    if parsed.scheme == "https":
        return NetBoxTarget(url=url, host=host, scheme="https", loopback_dev=False)

    if host not in LOOPBACK_HOSTS:
        raise ImproperlyConfigured(
            f"NETBOX_URL uses http:// with the remote host {host!r}. Plaintext to a remote NetBox "
            f"is not permitted: the token would cross the network in clear text. Use https://, or "
            f"a loopback host ({', '.join(sorted(LOOPBACK_HOSTS))}) for local development."
        )
    return NetBoxTarget(url=url, host=host, scheme="http", loopback_dev=True)


def warn_about_transport(target: NetBoxTarget, verify_ssl: bool) -> None:
    """Announce the two weakened-transport cases at startup (Requirements 2.3, 2.3a)."""
    if target.loopback_dev:
        logger.warning(
            "NetBox target %s uses plaintext http:// under the loopback development exception",
            target.display,
        )
    if not verify_ssl:
        logger.warning(
            "NETBOX_VERIFY_SSL is false: TLS certificates for %s are NOT verified. "
            "This is a lab-only setting.",
            target.display,
        )
