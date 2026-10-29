# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Interface-address assignments and explicit primary references (Requirement 8.13–8.26, D11).

The sheet never names an interface. Management is the unique `mgmt_only` interface (template, or
the device's own once it exists); loopback is the unique interface whose type is `loopback`. Both
families of one role land on that same interface. Primary references may select an address on any
interface of the device. A covering prefix is not required.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Any

from importer.validation import Code, RowError

ADDRESS_COLUMNS: tuple[str, ...] = (
    "loopback_ip4",
    "loopback_ip6",
    "management_ip4",
    "management_ip6",
)

PRIMARY_ADDRESS_COLUMNS: tuple[str, ...] = (
    "primary_address_v4",
    "primary_address_v6",
)

ROLE_FOR: dict[str, str] = {
    "loopback_ip4": "loopback",
    "loopback_ip6": "loopback",
    "management_ip4": "management",
    "management_ip6": "management",
}

FAMILY_FOR: dict[str, int] = {
    "loopback_ip4": 4,
    "loopback_ip6": 6,
    "management_ip4": 4,
    "management_ip6": 6,
    "primary_address_v4": 4,
    "primary_address_v6": 6,
}

# Last-resort names when the discriminator is absent on a community device type that still has
# the usual vendor port (Cumulus eth0 without mgmt_only, Loopback0 typed as virtual).
MGMT_NAMES = frozenset(
    {"eth0", "management1", "management0", "fxp0", "em0", "me0", "ma1", "mgmt"}
)
LOOPBACK_NAMES = frozenset({"lo", "lo0", "loopback0", "loopback1"})


@dataclass(frozen=True)
class ParsedAddress:
    column: str
    role: str
    family: int
    interface: ipaddress.IPv4Interface | ipaddress.IPv6Interface

    @property
    def cidr(self) -> str:
        return str(self.interface)


@dataclass(frozen=True)
class ParsedPrimaryAddress:
    column: str
    family: int
    interface: ipaddress.IPv4Interface | ipaddress.IPv6Interface

    @property
    def cidr(self) -> str:
        return str(self.interface)


@dataclass
class RoleMatch:
    """The unique interface for one role, or the candidates that made it unusable."""

    record: dict[str, Any] | None = None
    candidates: list[str] | None = None
    """None means zero matches; a list means more than one (ambiguous)."""

    @property
    def found(self) -> bool:
        return self.record is not None

    @property
    def ambiguous(self) -> bool:
        return self.candidates is not None

    @property
    def name(self) -> str:
        if self.record is None:
            return ""
        return str(self.record.get("name") or "")


def parse_device_ip(column: str, raw: str) -> ParsedAddress:
    """S2: a CIDR host with an explicit prefix, of the family the column names (8.13)."""
    parsed, family = _parse_ip(column, raw)
    return ParsedAddress(
        column=column, role=ROLE_FOR[column], family=family, interface=parsed
    )


def parse_primary_ip(column: str, raw: str) -> ParsedPrimaryAddress:
    """An explicit primary-IP reference, with the same CIDR rules as an assigned address."""
    parsed, family = _parse_ip(column, raw)
    return ParsedPrimaryAddress(column=column, family=family, interface=parsed)


def _parse_ip(
    column: str, raw: str
) -> tuple[ipaddress.IPv4Interface | ipaddress.IPv6Interface, int]:
    text = str(raw).strip()
    family = FAMILY_FOR[column]
    if "/" not in text:
        raise RowError(
            Code.DEVICE_IP_INVALID,
            column=column,
            value=text,
            expected=f"an IPv{family} CIDR with an explicit prefix length, e.g. "
            + ("192.0.2.1/32" if family == 4 else "2001:db8::1/128"),
            message=(
                f"{text!r} has no prefix length. Write the mask on the cell "
                f"({text}/32 or {text}/24) — it is not defaulted, because a silent /32 is how "
                f"two engineers end up with different addresses from the same sheet."
            ),
        )
    try:
        parsed = ipaddress.ip_interface(text)
    except ValueError:
        raise RowError(
            Code.DEVICE_IP_INVALID,
            column=column,
            value=text,
            expected=f"an IPv{family} CIDR with an explicit prefix length",
            message=f"{text!r} is not an IP address with a prefix length.",
        ) from None
    if parsed.version != family:
        raise RowError(
            Code.DEVICE_IP_FAMILY_MISMATCH,
            column=column,
            value=text,
            expected=f"an IPv{family} CIDR",
            message=(
                f"{column} must be IPv{family}, but {text!r} is IPv{parsed.version}. "
                f"Put it in the matching column rather than swapping families silently."
            ),
        )
    return parsed, family


def parse_row_addresses(row_get) -> list[ParsedAddress]:
    """Every non-blank address cell on one row. Blank cells are omitted, not parsed."""
    found: list[ParsedAddress] = []
    for column in ADDRESS_COLUMNS:
        raw = row_get(column)
        if not str(raw).strip():
            continue
        found.append(parse_device_ip(column, raw))
    return found


def parse_row_primary_addresses(row_get) -> list[ParsedPrimaryAddress]:
    """Every explicit primary reference on one row; blank means no request for that family."""
    found: list[ParsedPrimaryAddress] = []
    for column in PRIMARY_ADDRESS_COLUMNS:
        raw = row_get(column)
        if not str(raw).strip():
            continue
        found.append(parse_primary_ip(column, raw))
    return found


def canonical(value: Any) -> str:
    """Structured comparison so compressed and expanded IPv6 are the same address (8.24)."""
    return str(ipaddress.ip_interface(str(value).strip()))


def choice_value(raw: Any) -> str:
    if isinstance(raw, dict):
        return str(raw.get("value") or "")
    return str(raw or "")


def interface_name(record: dict[str, Any]) -> str:
    return str(record.get("name") or "")


def resolve_management(interfaces: list[dict[str, Any]]) -> RoleMatch:
    """Unique `mgmt_only`, else a unique well-known management name."""
    return _unique(
        [item for item in interfaces if item.get("mgmt_only")],
        [item for item in interfaces if interface_name(item).casefold() in MGMT_NAMES],
    )


def resolve_loopback(interfaces: list[dict[str, Any]]) -> RoleMatch:
    """Unique type `loopback`, else a unique well-known loopback name (community YAML)."""
    typed = [
        item
        for item in interfaces
        if choice_value(item.get("type")).casefold() == "loopback"
    ]
    named = [
        item for item in interfaces if interface_name(item).casefold() in LOOPBACK_NAMES
    ]
    return _unique(typed, named)


def resolve_roles(interfaces: list[dict[str, Any]]) -> dict[str, RoleMatch]:
    return {
        "management": resolve_management(interfaces),
        "loopback": resolve_loopback(interfaces),
    }


def _unique(primary: list[dict[str, Any]], fallback: list[dict[str, Any]]) -> RoleMatch:
    if len(primary) == 1:
        return RoleMatch(record=primary[0])
    if len(primary) > 1:
        return RoleMatch(candidates=sorted({interface_name(item) for item in primary}))
    if len(fallback) == 1:
        return RoleMatch(record=fallback[0])
    if len(fallback) > 1:
        return RoleMatch(candidates=sorted({interface_name(item) for item in fallback}))
    return RoleMatch()


def related_id(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, dict):
        object_id = raw.get("id")
        return int(object_id) if object_id is not None else None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
