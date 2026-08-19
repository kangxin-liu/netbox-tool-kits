# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Test-wide guard: no test reaches a real NetBox.

Pages now probe the target at load, so a test that renders one would otherwise open a socket to
whatever `NETBOX_URL` names. Blocking the transport keeps the suite hermetic and fast, and turns an
accidental live call into an obvious failure rather than a slow one.

`@responses.activate` patches the same adapter *inside* the test body, so it takes precedence over
this fixture wherever a test stubs HTTP deliberately.
"""

import csv
import io

import pytest
import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile

from importer.columns import columns_for
from importer.services import netbox_client
from importer.tests.fakes import FakeNetBox


@pytest.fixture(autouse=True)
def no_live_netbox(monkeypatch):
    def refuse(self, request, *args, **kwargs):
        raise requests.exceptions.ConnectionError(
            f"This test tried to reach {request.url} for real. Stub it with `responses` instead."
        )

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", refuse)


@pytest.fixture(autouse=True)
def empty_plan_cache():
    """Plans live in the cache, which is process-wide: one test's plan must not reach the next."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def netbox(monkeypatch) -> FakeNetBox:
    """The in-memory NetBox every view in a test talks to.

    Patched at `build_client` because that is the single place a client is constructed, so one patch
    covers the page probe, validation, and apply.
    """
    fake = FakeNetBox()
    monkeypatch.setattr(netbox_client, "build_client", lambda: fake)
    return fake


@pytest.fixture
def operator(db):
    return get_user_model().objects.create_superuser(
        username="operator", email="operator@example.com", password="synthetic-password"
    )


@pytest.fixture
def browser(client, operator):
    """A logged-in superuser's browser. Nothing in the suite uses real credentials."""
    client.force_login(operator)
    return client


def upload_of(importer: str, rows: list[dict[str, str]], filename: str = "") -> SimpleUploadedFile:
    """A real csv upload, built from the same column specs the templates are built from."""
    specs = columns_for(importer)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([spec.label for spec in specs])
    for row in rows:
        writer.writerow([row.get(spec.key, "") for spec in specs])
    payload = buffer.getvalue().encode("utf-8-sig")
    return SimpleUploadedFile(filename or f"{importer}.csv", payload, content_type="text/csv")
