# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Settings used by the test suite.

`config.settings` refuses to import without its required variables, which is exactly the behaviour
task 2 asks for — so the suite supplies obviously synthetic values here and then imports the real
settings unchanged. Every value below is fake: no real host, no real credential, nothing that
resembles a NetBox token (Requirement 14.6).

Tests that need a different target (a loopback URL, verification disabled) use
`django.test.override_settings` rather than editing this file.
"""

import os

SYNTHETIC_ENV = {
    "DJANGO_SECRET_KEY": "test-only-not-a-real-secret-key",
    "DJANGO_DEBUG": "false",
    "DJANGO_ALLOWED_HOSTS": "testserver,localhost",
    # The test client speaks plain http, so cookies must not be secure-only here.
    "DJANGO_SERVED_OVER_TLS": "false",
    "NETBOX_URL": "https://netbox.example.com/",
    "NETBOX_TOKEN": "0000000000000000000000000000000000000000",
    "NETBOX_VERIFY_SSL": "true",
    "NETBOX_DEFAULT_DEVICE_ROLE": "unassigned",
    "NETBOX_AUTOCREATE_DEVICE_TYPES": "false",
}
os.environ.update(SYNTHETIC_ENV)

from config.settings import *  # noqa: E402  (deliberately inherits the real settings)
