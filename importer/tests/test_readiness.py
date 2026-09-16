# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Task 9a — readiness summaries and connection status.

Requirements 2.4, 2.5, 2.7, 17.2–17.4, 17.7.
"""

import pytest
import requests
import responses
from django.test import override_settings
from django.urls import reverse

from importer.services import netbox_client, readiness

BASE = "https://netbox.example.com"
WRITE_METHODS = ("POST", "PATCH", "PUT", "DELETE")


@pytest.fixture
def netbox():
    return netbox_client.NetBoxClient(
        f"{BASE}/",
        "0000000000000000000000000000000000000000",
        verify_ssl=True,
        timeout=7,
        host="netbox.example.com",
    )


def stub_status(version="4.6.8"):
    responses.add(responses.GET, f"{BASE}/api/status/", json={"netbox-version": version})


def stub_count(path, count, **_):
    responses.add(
        responses.GET,
        f"{BASE}/api/{path}/",
        json={"count": count, "next": None, "previous": None, "results": []},
    )


def stub_ports(devices=12, interfaces=400, front=32, rear=8):
    """The four counts the cabling panel reads, in the order it reads them."""
    stub_count("dcim/devices", devices)
    stub_count("dcim/interfaces", interfaces)
    stub_count("dcim/front-ports", front)
    stub_count("dcim/rear-ports", rear)


def no_writes_were_issued():
    return [call for call in responses.calls if call.request.method in WRITE_METHODS] == []


class TestConnectionStatus:
    @responses.activate
    def test_a_healthy_target_reports_its_version(self, netbox):
        stub_status("4.6.8")
        status = readiness.connection_status(netbox)
        assert status.reachable and status.can_import
        assert status.label == "NetBox 4.6.8"

    @responses.activate
    def test_an_unsupported_version_disables_importing(self, netbox):
        """Requirement 2.7."""
        stub_status("4.5.9")
        status = readiness.connection_status(netbox)
        assert status.reachable
        assert status.can_import is False
        assert "4.5.9" in status.detail and ">=4.6, <4.7" in status.detail

    @responses.activate
    def test_an_unreachable_target_names_the_failure_category(self, netbox):
        """Requirement 2.5: host and category, never a credential."""
        responses.add(
            responses.GET, f"{BASE}/api/status/", body=requests.exceptions.SSLError("bad cert")
        )
        status = readiness.connection_status(netbox)
        assert status.reachable is False
        assert status.can_import is False
        assert status.category is netbox_client.Failure.TLS
        assert status.host == "netbox.example.com"
        assert "0000" not in status.detail

    @responses.activate
    def test_an_unreachable_target_is_logged_without_the_token(self, netbox, caplog):
        responses.add(
            responses.GET, f"{BASE}/api/status/", body=requests.exceptions.ConnectTimeout("slow")
        )
        with caplog.at_level("WARNING", logger="importer.netbox"):
            readiness.connection_status(netbox)
        assert "category=timeout" in caplog.text
        assert "0000000000" not in caplog.text


class TestHierarchyReadiness:
    @responses.activate
    def test_the_hierarchy_importer_needs_no_prerequisites(self, netbox):
        """Requirement 17.7: sheet 1 works against a completely empty instance."""
        summary = readiness.readiness_for("hierarchy", netbox)
        assert summary.ready is True
        assert summary.blockers == []
        assert len(responses.calls) == 0


class TestDevicesReadiness:
    @responses.activate
    def test_an_empty_instance_blocks_with_an_explanation(self, netbox):
        """Requirement 17.1/17.2: say what is missing rather than appearing broken."""
        stub_count("dcim/device-types", 0)
        stub_count("dcim/device-roles", 0)
        stub_count("dcim/interface-templates", 0)
        stub_count("dcim/interface-templates", 0)
        summary = readiness.readiness_for("devices", netbox)
        assert summary.ready is False
        assert any("no device types" in blocker for blocker in summary.blockers)
        assert any("no device roles" in blocker for blocker in summary.blockers)
        assert no_writes_were_issued()

    @responses.activate
    @override_settings(NETBOX_DEFAULT_DEVICE_ROLE="leaf-switch")
    def test_a_prepared_instance_is_ready(self, netbox):
        stub_count("dcim/device-types", 6)
        stub_count("dcim/device-roles", 1)  # the default-role lookup
        stub_count("dcim/device-roles", 4)  # the total
        stub_count("dcim/interface-templates", 4)
        stub_count("dcim/interface-templates", 4)
        summary = readiness.readiness_for("devices", netbox)
        assert summary.ready is True
        assert summary.blockers == []
        types = next(fact for fact in summary.facts if fact.label == "Device types defined")
        assert types.value == "6"

    @responses.activate
    @override_settings(NETBOX_DEFAULT_DEVICE_ROLE="no-such-role")
    def test_a_default_role_that_does_not_resolve_warns_at_page_load(self, netbox):
        """Requirement 17.3: warned here, not only when a sheet is validated."""
        stub_count("dcim/device-types", 6)
        stub_count("dcim/device-roles", 0)  # the default-role lookup finds nothing
        stub_count("dcim/device-roles", 4)  # roles do exist, just not that one
        stub_count("dcim/interface-templates", 0)
        stub_count("dcim/interface-templates", 0)
        summary = readiness.readiness_for("devices", netbox)
        role = next(fact for fact in summary.facts if fact.label == "Default device role")
        assert role.level == "warn"
        assert "no-such-role" in role.detail
        # A missing default is not fatal: a sheet naming its own role still works.
        assert summary.ready is True

    @responses.activate
    @override_settings(NETBOX_DEFAULT_DEVICE_ROLE="")
    def test_an_unset_default_role_is_reported_as_a_warning(self, netbox):
        stub_count("dcim/device-types", 6)
        stub_count("dcim/device-roles", 4)
        stub_count("dcim/interface-templates", 0)
        stub_count("dcim/interface-templates", 0)
        summary = readiness.readiness_for("devices", netbox)
        role = next(fact for fact in summary.facts if fact.label == "Default device role")
        assert role.level == "warn"
        assert "unset" in role.detail


class TestCablingReadiness:
    @responses.activate
    def test_an_instance_with_no_devices_blocks(self, netbox):
        stub_count("dcim/devices", 0)
        stub_count("dcim/interfaces", 0)
        stub_count("dcim/front-ports", 0)
        stub_count("dcim/rear-ports", 0)
        summary = readiness.readiness_for("cabling", netbox)
        assert summary.ready is False
        assert any("no devices" in blocker for blocker in summary.blockers)

    @responses.activate
    def test_devices_without_ports_are_called_out(self, netbox):
        """A device type with no port templates produces a device nothing can terminate on."""
        stub_count("dcim/devices", 12)
        stub_count("dcim/interfaces", 0)
        stub_count("dcim/front-ports", 0)
        stub_count("dcim/rear-ports", 0)
        summary = readiness.readiness_for("cabling", netbox)
        assert summary.ready is False
        assert any("no ports at all" in blocker for blocker in summary.blockers)

    @responses.activate
    def test_panel_ports_are_reported_front_and_rear(self, netbox):
        """Requirement 17.2: whether devices with front or rear ports exist."""
        stub_count("dcim/devices", 12)
        stub_count("dcim/interfaces", 400)
        stub_count("dcim/front-ports", 32)
        stub_count("dcim/rear-ports", 8)
        summary = readiness.readiness_for("cabling", netbox)
        assert summary.ready is True
        panel = next(fact for fact in summary.facts if "Patch-panel" in fact.label)
        assert panel.value == "32 / 8"

    @responses.activate
    def test_the_autocreate_flag_is_shown_as_off_by_default(self, netbox):
        """Requirement 17.2: the flag decides what an unknown port name does, so say which."""
        stub_ports()
        summary = readiness.readiness_for("cabling", netbox)
        flag = next(fact for fact in summary.facts if fact.label == "Create missing interfaces")
        assert flag.value == "off"
        assert "blocks the file" in flag.detail

    @responses.activate
    @override_settings(NETBOX_AUTOCREATE_INTERFACES=True)
    def test_the_autocreate_flag_warns_when_it_is_on(self, netbox):
        """It is not an error state, but it is one an operator should notice before uploading."""
        stub_ports()
        summary = readiness.readiness_for("cabling", netbox)
        flag = next(fact for fact in summary.facts if fact.label == "Create missing interfaces")
        assert flag.value == "on"
        assert flag.level == "warn"
        assert summary.ready is True

    @responses.activate
    def test_a_transport_failure_becomes_a_blocker_not_a_crash(self, netbox):
        responses.add(
            responses.GET,
            f"{BASE}/api/dcim/devices/",
            body=requests.exceptions.ConnectionError("Connection refused"),
        )
        summary = readiness.readiness_for("cabling", netbox)
        assert summary.ready is False
        assert summary.blockers


def test_an_unknown_importer_is_rejected(netbox):
    with pytest.raises(ValueError, match="unknown importer"):
        readiness.readiness_for("ipam", netbox)


# --- through the page -----------------------------------------------------------------------


@pytest.mark.django_db
class TestPageIntegration:
    @pytest.fixture
    def signed_in(self, client, django_user_model):
        user = django_user_model.objects.create_superuser(
            username="operator", password="synthetic-test-password"
        )
        client.force_login(user)
        return client

    @responses.activate
    def test_the_header_shows_the_version_on_every_page(self, signed_in):
        stub_status("4.6.8")
        html = signed_in.get(reverse("importer:hierarchy")).content.decode()
        assert "NetBox 4.6.8" in html
        assert no_writes_were_issued()

    @responses.activate
    def test_an_unreachable_target_is_diagnosed_on_the_page(self, signed_in):
        responses.add(
            responses.GET,
            f"{BASE}/api/status/",
            body=requests.exceptions.ConnectionError("Name or service not known"),
        )
        html = signed_in.get(reverse("importer:devices")).content.decode()
        assert "Cannot reach NetBox" in html
        assert "dns" in html
        assert "Importing is disabled" in html
        # And no prerequisite probing happened, since there is nothing to probe.
        assert "Prerequisites in NetBox" not in html

    @responses.activate
    @override_settings(NETBOX_DEFAULT_DEVICE_ROLE="no-such-role")
    def test_the_devices_page_warns_about_the_default_role_at_load(self, signed_in):
        stub_status("4.6.8")
        stub_count("dcim/device-types", 6)
        stub_count("dcim/device-roles", 0)
        stub_count("dcim/device-roles", 4)
        stub_count("dcim/interface-templates", 0)
        stub_count("dcim/interface-templates", 0)
        html = signed_in.get(reverse("importer:devices")).content.decode()
        assert "no-such-role" in html
        assert "Prerequisites in NetBox" in html
        assert no_writes_were_issued()

    @responses.activate
    def test_an_unsupported_version_blocks_importing_on_the_page(self, signed_in):
        stub_status("4.7.0")
        html = signed_in.get(reverse("importer:cabling")).content.decode()
        assert "Importing is disabled" in html
        assert "4.7.0" in html
