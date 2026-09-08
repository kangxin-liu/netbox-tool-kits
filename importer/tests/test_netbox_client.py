# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Task 8 / 9a — client factory, version gate, failure classification, redaction.

Requirements 2.5, 2.6, 2.7, 12.7, 14.1, 14.5. No live NetBox: HTTP is stubbed with `responses`.
"""

import socket

import pytest
import requests
import responses
from pynetbox.core.query import RequestError

from importer.services import netbox_client
from importer.services.netbox_client import (
    Failure,
    NetBoxUnavailable,
    NetBoxWriteRejected,
    build_client,
)

BASE = "https://netbox.example.com"
TOKEN = "0000000000000000000000000000000000000000"


@pytest.fixture
def client():
    return netbox_client.NetBoxClient(
        f"{BASE}/", TOKEN, verify_ssl=True, timeout=7, host="netbox.example.com"
    )


def stub_status(version: str = "4.6.8") -> None:
    responses.add(
        responses.GET,
        f"{BASE}/api/status/",
        json={"netbox-version": version, "django-version": "5.2.17"},
        status=200,
    )


class FakeResponse:
    """The shape `pynetbox.RequestError` reads: enough to classify, and nothing more."""

    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self.reason = "Error"
        self.url = f"{BASE}/api/dcim/racks/"
        self.text = text or "error"
        self._payload = payload

        class _Request:
            body = None

        self.request = _Request()

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


# --- version gate ---------------------------------------------------------------------------


class TestVersionGate:
    @pytest.mark.parametrize("version", ["4.6", "4.6.0", "4.6.8", "4.6.12"])
    def test_versions_in_range_are_accepted(self, version):
        supported, reason = netbox_client.check_version(version)
        assert supported and reason == ""

    @pytest.mark.parametrize("version", ["4.5.9", "4.4.0", "4.7.0", "5.0.1", "3.7.8"])
    def test_versions_outside_the_range_are_refused(self, version):
        supported, reason = netbox_client.check_version(version)
        assert not supported
        # Requirement 2.7: name the detected and the supported versions.
        assert version in reason
        assert ">=4.6, <4.7" in reason

    @pytest.mark.parametrize("version", ["", "unknown", "not.a.version"])
    def test_an_unreadable_version_is_refused_rather_than_assumed(self, version):
        supported, reason = netbox_client.check_version(version)
        assert not supported
        assert ">=4.6, <4.7" in reason

    @responses.activate
    def test_status_records_the_version_and_probes_once(self, client):
        """Requirement 2.6: the version comes from /api/status/ and is cached per client."""
        stub_status("4.6.8")
        first = client.status()
        second = client.status()
        assert first.version == "4.6.8"
        assert first.supported is True
        assert second is first
        assert len(responses.calls) == 1

    @responses.activate
    def test_an_out_of_range_instance_reports_why(self, client):
        stub_status("4.5.9")
        status = client.status()
        assert status.supported is False
        assert "4.5.9" in status.reason and ">=4.6, <4.7" in status.reason

    @responses.activate
    def test_the_version_is_logged_without_the_token(self, client, caplog):
        stub_status()
        with caplog.at_level("INFO", logger="importer.netbox"):
            client.status()
        assert "version=4.6.8" in caplog.text
        assert TOKEN not in caplog.text


# --- failure classification -----------------------------------------------------------------


class TestClassification:
    def test_a_tls_failure_is_reported_as_tls(self):
        failure = netbox_client.classify(
            requests.exceptions.SSLError("certificate verify failed"), "netbox.example.com"
        )
        assert failure.category is Failure.TLS
        assert "certificate" in failure.operator_message

    def test_a_timeout_is_reported_as_a_timeout(self):
        failure = netbox_client.classify(
            requests.exceptions.ConnectTimeout("too slow"), "netbox.example.com"
        )
        assert failure.category is Failure.TIMEOUT

    def test_an_unresolvable_host_is_reported_as_dns(self):
        error = requests.exceptions.ConnectionError("Name or service not known")
        assert netbox_client.classify(error, "netbox.example.com").category is Failure.DNS

    def test_a_gaierror_cause_is_also_recognised_as_dns(self):
        error = requests.exceptions.ConnectionError("failed")
        error.__cause__ = socket.gaierror(8, "nodename nor servname provided")
        assert netbox_client.classify(error, "netbox.example.com").category is Failure.DNS

    def test_a_refused_connection_is_reported_as_a_connection_failure(self):
        error = requests.exceptions.ConnectionError("Connection refused")
        assert netbox_client.classify(error, "netbox.example.com").category is Failure.CONNECTION

    @pytest.mark.parametrize("status", [401, 403])
    def test_a_rejected_token_is_reported_as_auth(self, status):
        failure = netbox_client.classify(RequestError(FakeResponse(status)), "netbox.example.com")
        assert failure.category is Failure.AUTH
        assert "token" in failure.operator_message

    def test_a_server_error_is_reported_as_a_server_failure(self):
        failure = netbox_client.classify(RequestError(FakeResponse(500)), "netbox.example.com")
        assert failure.category is Failure.SERVER

    def test_an_unexpected_exception_gets_a_generic_message(self):
        failure = netbox_client.classify(ValueError("internal detail leaks here"), "nb.example.com")
        assert failure.category is Failure.CONNECTION
        assert "internal detail" not in failure.operator_message

    @responses.activate
    def test_a_transport_failure_surfaces_as_netbox_unavailable(self, client):
        responses.add(
            responses.GET,
            f"{BASE}/api/status/",
            body=requests.exceptions.ConnectTimeout("timed out"),
        )
        with pytest.raises(NetBoxUnavailable) as raised:
            client.status()
        assert raised.value.category is Failure.TIMEOUT


class TestRedaction:
    def test_the_token_is_scrubbed_from_any_text(self):
        assert TOKEN not in netbox_client.redact(f"Authorization: Token {TOKEN}")
        assert "[redacted]" in netbox_client.redact(f"Token {TOKEN}")

    @responses.activate
    def test_no_classified_failure_carries_the_token_or_a_traceback(self, client):
        responses.add(responses.GET, f"{BASE}/api/status/", json={"detail": "nope"}, status=403)
        with pytest.raises(NetBoxUnavailable) as raised:
            client.status()
        text = str(raised.value) + raised.value.operator_message
        assert TOKEN not in text
        assert "Traceback" not in text
        assert "Authorization" not in text


# --- reads and writes -----------------------------------------------------------------------


class TestReads:
    @responses.activate
    def test_get_all_follows_pagination(self, client):
        responses.add(
            responses.GET,
            f"{BASE}/api/dcim/racks/",
            json={
                "count": 2,
                "next": f"{BASE}/api/dcim/racks/?limit=1&offset=1",
                "previous": None,
                "results": [{"id": 1, "name": "EXA1-DH1-R01", "u_height": 45}],
            },
        )
        responses.add(
            responses.GET,
            f"{BASE}/api/dcim/racks/",
            json={
                "count": 2,
                "next": None,
                "previous": None,
                "results": [{"id": 2, "name": "EXA1-DH1-R02", "u_height": 42}],
            },
        )
        racks = client.get_all("dcim.racks", site="exa1")
        assert [rack["name"] for rack in racks] == ["EXA1-DH1-R01", "EXA1-DH1-R02"]

    @responses.activate
    def test_nested_objects_arrive_as_plain_dicts(self, client):
        responses.add(
            responses.GET,
            f"{BASE}/api/dcim/racks/",
            json={
                "count": 1,
                "next": None,
                "results": [
                    {
                        "id": 1,
                        "name": "EXA1-DH1-R01",
                        "site": {"id": 3, "name": "EXA1", "slug": "exa1"},
                    }
                ],
            },
        )
        rack = client.get_all("dcim.racks")[0]
        assert rack["site"]["id"] == 3

    @responses.activate
    def test_choices_come_from_the_api_and_are_cached(self, client):
        """Requirement 7.7: never a hardcoded list."""
        responses.add(
            responses.OPTIONS,
            f"{BASE}/api/dcim/racks/",
            json={
                "actions": {
                    "POST": {
                        "status": {"choices": [{"value": "active"}, {"value": "planned"}]},
                        "width": {"choices": [{"value": 19}, {"value": 23}]},
                    }
                }
            },
        )
        assert client.choices_for("dcim.racks", "status") == frozenset({"active", "planned"})
        assert client.choices_for("dcim.racks", "width") == frozenset({"19", "23"})
        assert client.choices_for("dcim.racks", "no_such_field") == frozenset()
        assert len(responses.calls) == 1


class TestCreates:
    @responses.activate
    def test_a_list_post_returns_every_created_record(self, client):
        responses.add(
            responses.POST,
            f"{BASE}/api/dcim/racks/",
            json=[{"id": 7, "name": "EXA1-DH1-R01"}, {"id": 8, "name": "EXA1-DH1-R02"}],
            status=201,
        )
        created = client.create("dcim.racks", [{"name": "EXA1-DH1-R01"}, {"name": "EXA1-DH1-R02"}])
        assert [record["id"] for record in created] == [7, 8]

    @responses.activate
    def test_a_field_level_rejection_exposes_the_per_item_array(self, client):
        """Decision D7: a positionally aligned body maps straight to a spreadsheet line."""
        responses.add(
            responses.POST,
            f"{BASE}/api/dcim/racks/",
            json=[{}, {"name": ["This field is required."]}, {}],
            status=400,
        )
        with pytest.raises(NetBoxWriteRejected) as raised:
            client.create("dcim.racks", [{"name": "a"}, {}, {"name": "c"}])
        per_item = raised.value.per_item
        assert per_item is not None
        assert per_item[1]["name"] == ["This field is required."]

    @responses.activate
    def test_an_unindexed_rejection_has_no_per_item_array(self, client):
        """The case that forces the row-by-row replay: NetBox says what, not which."""
        responses.add(
            responses.POST,
            f"{BASE}/api/dcim/devices/",
            json={"position": ["U10.0 is already occupied"]},
            status=400,
        )
        with pytest.raises(NetBoxWriteRejected) as raised:
            client.create("dcim.devices", [{"name": "a"}, {"name": "b"}])
        assert raised.value.per_item is None
        assert raised.value.body["position"] == ["U10.0 is already occupied"]

    @responses.activate
    def test_an_auth_failure_during_a_write_aborts_rather_than_blaming_a_row(self, client):
        responses.add(responses.POST, f"{BASE}/api/dcim/racks/", json={}, status=403)
        with pytest.raises(NetBoxUnavailable) as raised:
            client.create("dcim.racks", [{"name": "a"}])
        assert raised.value.category is Failure.AUTH


class TestCreateOnlyInvariant:
    """Requirement 11.3: the transport itself refuses to update or delete."""

    @pytest.mark.parametrize("method", ["PUT", "DELETE"])
    def test_the_session_refuses_modifying_verbs(self, client, method):
        session = client._api.http_session
        with pytest.raises(RuntimeError, match="only creates objects"):
            session.request(method, f"{BASE}/api/dcim/racks/1/")

    def test_the_client_exposes_patch_but_no_delete(self):
        surface = {name for name in dir(netbox_client.NetBoxClient) if not name.startswith("_")}
        assert "patch" in surface
        assert not surface & {"update", "delete", "save", "remove"}

    def test_patch_refuses_any_endpoint_other_than_devices(self, client):
        with pytest.raises(RuntimeError, match="only PATCHes"):
            client.patch("dcim.racks", {"id": 1, "primary_ip4": 1})

    def test_patch_refuses_fields_other_than_primary_ip(self, client):
        with pytest.raises(RuntimeError, match="primary_ip4"):
            client.patch("dcim.devices", {"id": 1, "primary_ip4": 1, "name": "leaf01"})


class TestFactory:
    def test_build_client_applies_the_configured_target(self, settings):
        client = build_client()
        assert client.host == settings.NETBOX_HOST
        assert client._api.http_session.timeout == settings.NETBOX_TIMEOUT
        assert client._api.http_session.verify is settings.NETBOX_VERIFY_SSL

    @responses.activate
    def test_object_url_deep_links_into_netbox(self, client):
        assert client.object_url("dcim.racks", 12) == f"{BASE}/dcim/racks/12/"
