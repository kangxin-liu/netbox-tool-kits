# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Template context for the NetBox target banner (Requirement 2.4).

Exposes the host, the scheme, and the two weakened-transport flags. It does not expose the token,
and there is no code path from a template to `settings.NETBOX_TOKEN`.
"""

from typing import Any

from django.conf import settings
from django.http import HttpRequest


def netbox_target(request: HttpRequest) -> dict[str, Any]:
    return {
        "netbox_host": settings.NETBOX_HOST,
        "netbox_scheme": settings.NETBOX_TARGET.scheme,
        "netbox_display": settings.NETBOX_TARGET.display,
        "netbox_loopback_dev": settings.NETBOX_LOOPBACK_DEV,
        "netbox_verify_ssl": settings.NETBOX_VERIFY_SSL,
    }
