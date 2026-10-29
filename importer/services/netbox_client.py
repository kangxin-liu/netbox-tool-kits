# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""NetBox client factory, version probe, and failure classification.

The token crosses this boundary and no other (`design.md`, trust boundary 3). Everything this module
raises is redacted first, so no exception, log line, or template can carry the token, a request
header, or a traceback out (Requirements 2.5, 12.7, 14.1).

Failures are classified rather than surfaced raw, because "DNS does not resolve netbox.example.com"
and "the token was rejected" need different actions from the operator, and a bare
`ConnectionError(...)` distinguishes them for nobody.
"""

import logging
import socket
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import pynetbox
import requests
from django.conf import settings
from pynetbox.core.query import RequestError

logger = logging.getLogger("importer.netbox")

FORWARD_METHODS = frozenset({"GET", "OPTIONS", "HEAD", "POST", "PATCH"})
REVERT_METHODS = frozenset({"GET", "OPTIONS", "HEAD", "PATCH", "DELETE"})

PRIMARY_IP_PATCH_FIELDS = frozenset({"id", "primary_ip4", "primary_ip6"})
INTERFACE_TAG_PATCH_FIELDS = frozenset({"id", "tags"})
RACK_ROLE_PATCH_FIELDS = frozenset({"id", "role"})


class Failure(StrEnum):
    """The categories Requirement 2.5 asks to be told apart."""

    DNS = "dns"
    TLS = "tls"
    TIMEOUT = "timeout"
    AUTH = "auth"
    CONNECTION = "connection"
    VERSION = "version"
    SERVER = "server"


class NetBoxUnavailable(Exception):
    """A connection-level failure, already classified and redacted.

    The redaction happens here rather than only at the classifier, so the guarantee belongs to the
    type: every construction site, including ones written later, produces a message that is safe to
    show and log (Requirement 14.1).
    """

    def __init__(self, category: Failure, host: str, detail: str) -> None:
        detail = redact(detail)
        super().__init__(f"{category.value}: {detail}")
        self.category = category
        self.host = host
        self.detail = detail

    @property
    def operator_message(self) -> str:
        return self.detail


class NetBoxWriteRejected(Exception):
    """NetBox refused a write. Carries the parsed body so a row can be blamed (Req 12.2).

    The body is somebody else's text, and it ends up in the run report and the audit rows verbatim.
    A server or proxy that echoes the request's headers back in its error body would otherwise
    persist the token, so the body is redacted on the way in (Requirement 14.1).
    """

    def __init__(self, status_code: int, body: Any, endpoint: str) -> None:
        super().__init__(f"NetBox rejected a write on {endpoint} with HTTP {status_code}")
        self.status_code = status_code
        self.body = redact_deeply(body)
        self.endpoint = endpoint

    @property
    def per_item(self) -> list[dict[str, Any]] | None:
        """The body as a per-item array aligned to the submitted list, when NetBox returned one.

        A field-level error comes back positionally aligned to the items posted, which maps straight
        to a spreadsheet line. A model `clean()` error comes back as a single un-indexed object, and
        the caller then has to replay the batch row by row to attribute it (`design.md`, D7).
        """
        if isinstance(self.body, list) and all(isinstance(item, dict) for item in self.body):
            return self.body
        return None


def redact(text: str) -> str:
    """Strip anything token-shaped out of a string before it can be shown or logged.

    The token should never reach here — nothing formats it into a message — so this is the second
    line of defence rather than the first (Requirement 14.1).
    """
    cleaned = text
    for token in {
        getattr(settings, "NETBOX_TOKEN", ""),
        getattr(settings, "NETBOX_REVERT_TOKEN", ""),
    }:
        if token:
            cleaned = cleaned.replace(token, "[redacted]")
    for marker in ("Authorization", "Token "):
        if marker in cleaned:
            cleaned = cleaned.replace(marker, "[redacted]")
    return cleaned


def redact_deeply(value: Any) -> Any:
    """`redact` over a parsed JSON body, keeping its shape so per-item blame still works."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_deeply(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_deeply(item) for key, item in value.items()}
    return value


class _TimeoutSession(requests.Session):
    """Applies `NETBOX_TIMEOUT` to every request, which `pynetbox` does not expose directly."""

    def __init__(self, timeout: int, verify: bool, permitted_methods: frozenset[str]) -> None:
        super().__init__()
        self.timeout = timeout
        self.verify = verify
        self.permitted_methods = permitted_methods

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        if method.upper() not in self.permitted_methods:
            # Belt and braces: callers only get the explicit methods exposed by their client class.
            raise RuntimeError(f"{method} is not permitted by the NetBox transport.")
        return super().request(method, url, **kwargs)


@dataclass(frozen=True)
class NetBoxStatus:
    """What `/api/status/` told us, plus whether we can import against it (Req 2.6, 2.7)."""

    version: str
    host: str
    supported: bool
    reason: str = ""

    @property
    def version_tuple(self) -> tuple[int, ...]:
        return parse_version(self.version)


def parse_version(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in (version or "").split(".")[:3]:
        digits = "".join(char for char in chunk if char.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def supported_range() -> str:
    low = ".".join(str(part) for part in settings.NETBOX_MIN_VERSION)
    high = ".".join(str(part) for part in settings.NETBOX_MAX_VERSION_EXCLUSIVE)
    return f">={low}, <{high}"


def check_version(version: str) -> tuple[bool, str]:
    """Requirement 2.7: refuse a version outside the supported range, naming both."""
    parsed = parse_version(version)
    if not parsed or parsed == (0,):
        return False, (
            f"NetBox did not report a usable version (got {version!r}). "
            f"This tool supports {supported_range()}."
        )
    minimum = settings.NETBOX_MIN_VERSION
    maximum = settings.NETBOX_MAX_VERSION_EXCLUSIVE
    if parsed[: len(minimum)] < minimum or parsed[: len(maximum)] >= maximum:
        return False, (
            f"NetBox {version} is outside the supported range {supported_range()}. "
            f"Importing was refused rather than risking a failure partway through on a "
            f"renamed field."
        )
    return True, ""


def classify(exception: Exception, host: str) -> NetBoxUnavailable:
    """Map a transport failure onto an operator-facing category (Requirement 2.5)."""
    if isinstance(exception, RequestError):
        status = getattr(exception.req, "status_code", 0)
        if status in {401, 403}:
            return NetBoxUnavailable(
                Failure.AUTH,
                host,
                f"NetBox at {host} rejected the API token (HTTP {status}). Check that the token "
                f"is current and has DCIM write permission.",
            )
        return NetBoxUnavailable(
            Failure.SERVER,
            host,
            f"NetBox at {host} returned HTTP {status}.",
        )

    if isinstance(exception, requests.exceptions.SSLError):
        return NetBoxUnavailable(
            Failure.TLS,
            host,
            f"The TLS certificate for {host} could not be verified. Fix the certificate rather "
            f"than disabling verification against anything but a lab instance.",
        )

    if isinstance(exception, requests.exceptions.Timeout):
        return NetBoxUnavailable(
            Failure.TIMEOUT,
            host,
            f"NetBox at {host} did not respond within {settings.NETBOX_TIMEOUT} seconds.",
        )

    if isinstance(exception, requests.exceptions.ConnectionError):
        text = str(exception)
        if _looks_like_dns(exception, text):
            return NetBoxUnavailable(
                Failure.DNS,
                host,
                f"The host {host} could not be resolved. Check NETBOX_URL and your DNS.",
            )
        return NetBoxUnavailable(
            Failure.CONNECTION,
            host,
            f"Could not open a connection to {host}. Check that NetBox is running and reachable.",
        )

    # Deliberately generic: an unexpected exception's text is not an operator-facing message.
    return NetBoxUnavailable(
        Failure.CONNECTION, host, f"An unexpected error occurred talking to {host}."
    )


def _looks_like_dns(exception: Exception, text: str) -> bool:
    if isinstance(getattr(exception, "__cause__", None), socket.gaierror):
        return True
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in ("name or service not known", "nodename nor servname", "getaddrinfo failed")
    )


class NetBoxClient:
    """Reads/creates plus the field-scoped forward PATCH exceptions."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        verify_ssl: bool,
        timeout: int,
        host: str,
        permitted_methods: frozenset[str] = FORWARD_METHODS,
    ) -> None:
        self.host = host
        self._api = pynetbox.api(url, token=token)
        self._api.http_session = _TimeoutSession(
            timeout=timeout,
            verify=verify_ssl,
            permitted_methods=permitted_methods,
        )
        self._status: NetBoxStatus | None = None
        self._choices: dict[str, dict[str, frozenset[str]]] = {}
        self.request_count = 0

    # --- plumbing ---------------------------------------------------------------------------

    def _endpoint(self, endpoint: str) -> Any:
        """`"dcim.racks"` → the pynetbox endpoint object."""
        app, _, name = endpoint.partition(".")
        return getattr(getattr(self._api, app), name.replace("-", "_"))

    def status(self, *, refresh: bool = False) -> NetBoxStatus:
        """Probe `/api/status/` once per client and cache it (Requirements 2.6, 2.7)."""
        if self._status is not None and not refresh:
            return self._status
        try:
            self.request_count += 1
            payload = self._api.status()
        except Exception as exception:
            raise classify(exception, self.host) from None

        version = str(payload.get("netbox-version", ""))
        supported, reason = check_version(version)
        self._status = NetBoxStatus(
            version=version, host=self.host, supported=supported, reason=reason
        )
        logger.info("netbox host=%s version=%s supported=%s", self.host, version, supported)
        return self._status

    def get_all(self, endpoint: str, **filters: Any) -> list[dict[str, Any]]:
        """Every matching record as plain dicts. Pagination is pynetbox's problem."""
        try:
            self.request_count += 1
            records = (
                self._endpoint(endpoint).filter(**filters)
                if filters
                else (self._endpoint(endpoint).all())
            )
            return [dict(record) for record in records]
        except RequestError as exception:
            raise classify(exception, self.host) from None
        except Exception as exception:
            raise classify(exception, self.host) from None

    def get_one(self, endpoint: str, object_id: int) -> dict[str, Any] | None:
        """Read one object by id for mutation finalisation and revert drift checks."""
        try:
            self.request_count += 1
            record = self._endpoint(endpoint).get(int(object_id))
            return dict(record) if record is not None else None
        except RequestError as exception:
            if getattr(exception.req, "status_code", 0) == 404:
                return None
            raise classify(exception, self.host) from None
        except Exception as exception:
            raise classify(exception, self.host) from None

    def count(self, endpoint: str, **filters: Any) -> int:
        """How many objects match, without fetching them — for the readiness summaries."""
        try:
            self.request_count += 1
            return int(self._endpoint(endpoint).count(**filters))
        except Exception as exception:
            raise classify(exception, self.host) from None

    def choices_for(self, endpoint: str, field: str) -> frozenset[str]:
        """Choice values as NetBox reports them, cached per client (Requirement 7.7).

        Read from the API rather than hardcoded, so a value NetBox accepts is never rejected here
        because a list in our source went stale.
        """
        cached = self._choices.get(endpoint)
        if cached is None:
            try:
                self.request_count += 1
                raw = self._endpoint(endpoint).choices()
            except Exception as exception:
                raise classify(exception, self.host) from None
            cached = {
                name: frozenset(str(choice["value"]) for choice in values)
                for name, values in raw.items()
            }
            self._choices[endpoint] = cached
        return cached.get(field, frozenset())

    def create(self, endpoint: str, payload: dict | list[dict]) -> list[dict[str, Any]]:
        """One POST, whether given a single payload or a list.

        A list POST is atomic in NetBox (measured, decision D7), so a failure means nothing from
        this call was written.
        """
        try:
            self.request_count += 1
            created = self._endpoint(endpoint).create(payload)
        except RequestError as exception:
            status = getattr(exception.req, "status_code", 0)
            if status in {401, 403}:
                raise classify(exception, self.host) from None
            raise NetBoxWriteRejected(status, _body_of(exception), endpoint) from None
        except Exception as exception:
            raise classify(exception, self.host) from None

        if isinstance(created, list):
            return [dict(record) for record in created]
        return [dict(created)]

    def patch(self, endpoint: str, payload: dict | list[dict]) -> list[dict[str, Any]]:
        """The bounded forward PATCH exceptions.

        Any other endpoint or field is refused here, so a caller cannot accidentally reopen
        update-in-general (Requirements 8.19, 9.49, 11.3).
        """
        allowed = {
            "dcim.devices": PRIMARY_IP_PATCH_FIELDS,
            "dcim.interfaces": INTERFACE_TAG_PATCH_FIELDS,
            "dcim.racks": RACK_ROLE_PATCH_FIELDS,
        }
        permitted_fields = allowed.get(endpoint)
        if permitted_fields is None:
            raise RuntimeError(
                f"PATCH of {endpoint} is not permitted: forward apply only PATCHes device "
                f"primary IPs, interface tags, and rack roles."
            )
        payloads = payload if isinstance(payload, list) else [payload]
        for item in payloads:
            extra = set(item) - permitted_fields
            if extra:
                raise RuntimeError(
                    f"{endpoint} PATCH contains forbidden field(s): " + ", ".join(sorted(extra))
                )
            if "id" not in item:
                raise RuntimeError(f"{endpoint} PATCH needs the object id.")
            if endpoint == "dcim.devices" and not ("primary_ip4" in item or "primary_ip6" in item):
                raise RuntimeError("device PATCH needs primary_ip4 and/or primary_ip6.")
            if endpoint == "dcim.interfaces":
                if set(item) != INTERFACE_TAG_PATCH_FIELDS:
                    raise RuntimeError("interface PATCH must contain exactly id and tags.")
                if not isinstance(item["tags"], list) or not all(
                    isinstance(tag_id, int) for tag_id in item["tags"]
                ):
                    raise RuntimeError("interface PATCH tags must be a list of integer tag ids.")
            if endpoint == "dcim.racks":
                if set(item) != RACK_ROLE_PATCH_FIELDS:
                    raise RuntimeError("rack PATCH must contain exactly id and role.")
                if not isinstance(item["role"], int):
                    raise RuntimeError("rack PATCH role must be an integer rack-role id.")

        updated: list[dict[str, Any]] = []
        try:
            for item in payloads:
                self.request_count += 1
                object_id = int(item["id"])
                body = {key: value for key, value in item.items() if key != "id"}
                app, _, name = endpoint.partition(".")
                url = (
                    f"{str(self._api.base_url).rstrip('/')}/{app}/"
                    f"{name.replace('_', '-')}/{object_id}/"
                )
                response = self._api.http_session.patch(url, json=body)
                if not response.ok:
                    raise RequestError(response)
                payload = response.json() if response.content else {"id": object_id, **body}
                updated.append(dict(payload))
        except RequestError as exception:
            status = getattr(exception.req, "status_code", 0)
            if status in {401, 403}:
                raise classify(exception, self.host) from None
            raise NetBoxWriteRejected(status, _body_of(exception), endpoint) from None
        except Exception as exception:
            raise classify(exception, self.host) from None
        return updated

    def object_url(self, endpoint: str, object_id: int) -> str:
        """A deep link an operator can click (Requirements 12.1, 16.7)."""
        app, _, name = endpoint.partition(".")
        base = str(self._api.base_url).removesuffix("/api")
        return f"{base}/{app}/{name.replace('_', '-')}/{object_id}/"

    def _object_api_url(self, endpoint: str, object_id: int) -> str:
        app, _, name = endpoint.partition(".")
        return (
            f"{str(self._api.base_url).rstrip('/')}/{app}/"
            f"{name.replace('_', '-')}/{int(object_id)}/"
        )


class RevertNetBoxClient(NetBoxClient):
    """The only client surface that can issue inverse PATCH and DELETE requests (D14)."""

    def __init__(self, url: str, token: str, *, verify_ssl: bool, timeout: int, host: str) -> None:
        super().__init__(
            url,
            token,
            verify_ssl=verify_ssl,
            timeout=timeout,
            host=host,
            permitted_methods=REVERT_METHODS,
        )

    def restore(self, endpoint: str, object_id: int, fields: dict[str, Any]) -> dict[str, Any]:
        if not fields:
            raise RuntimeError("A revert PATCH needs at least one recorded field.")
        url = self._object_api_url(endpoint, object_id)
        try:
            self.request_count += 1
            response = self._api.http_session.patch(url, json=fields)
            if not response.ok:
                raise RequestError(response)
            payload = response.json() if response.content else {"id": object_id, **fields}
            return dict(payload)
        except RequestError as exception:
            status = getattr(exception.req, "status_code", 0)
            if status in {401, 403}:
                raise classify(exception, self.host) from None
            raise NetBoxWriteRejected(status, _body_of(exception), endpoint) from None
        except Exception as exception:
            raise classify(exception, self.host) from None

    def delete(self, endpoint: str, object_id: int) -> None:
        url = self._object_api_url(endpoint, object_id)
        try:
            self.request_count += 1
            response = self._api.http_session.delete(url)
            if not response.ok and response.status_code != 404:
                raise RequestError(response)
        except RequestError as exception:
            status = getattr(exception.req, "status_code", 0)
            if status in {401, 403}:
                raise classify(exception, self.host) from None
            raise NetBoxWriteRejected(status, _body_of(exception), endpoint) from None
        except Exception as exception:
            raise classify(exception, self.host) from None


def _body_of(exception: RequestError) -> Any:
    try:
        return exception.req.json()
    except ValueError:
        return redact(str(getattr(exception, "error", "")))[:500]


def build_client() -> NetBoxClient:
    """The one place a client is constructed, so configuration is applied consistently."""
    return NetBoxClient(
        settings.NETBOX_URL,
        settings.NETBOX_TOKEN,
        verify_ssl=settings.NETBOX_VERIFY_SSL,
        timeout=settings.NETBOX_TIMEOUT,
        host=settings.NETBOX_HOST,
    )


def build_revert_client() -> RevertNetBoxClient:
    """Build the isolated inverse-capable client, falling back to NETBOX_TOKEN by configuration."""
    return RevertNetBoxClient(
        settings.NETBOX_URL,
        settings.NETBOX_REVERT_TOKEN,
        verify_ssl=settings.NETBOX_VERIFY_SSL,
        timeout=settings.NETBOX_TIMEOUT,
        host=settings.NETBOX_HOST,
    )
