# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Task 9 — the lookup and choice cache. Requirements 11.1, 15.1, 15.2."""

import pytest
import responses

from importer.services import netbox_client
from importer.services.lookups import Lookups

BASE = "https://netbox.example.com"


@pytest.fixture
def client():
    return netbox_client.NetBoxClient(
        f"{BASE}/",
        "0000000000000000000000000000000000000000",
        verify_ssl=True,
        timeout=7,
        host="netbox.example.com",
    )


@pytest.fixture
def lookups(client):
    return Lookups(client=client)


def page(*results):
    return {"count": len(results), "next": None, "previous": None, "results": list(results)}


def stub(endpoint_path, *results, status=200):
    responses.add(responses.GET, f"{BASE}/api/{endpoint_path}/", json=page(*results), status=status)


class TestNaturalKeyLookups:
    @responses.activate
    def test_a_site_is_found_by_slug_or_name(self, lookups):
        stub("dcim/sites", {"id": 3, "name": "EXA1", "slug": "exa1"})
        lookups.prefetch("dcim.sites")
        assert lookups.get_or_none("dcim.sites", slug="exa1")["id"] == 3
        assert lookups.get_or_none("dcim.sites", name="EXA1")["id"] == 3

    @responses.activate
    def test_lookup_is_case_insensitive(self, lookups):
        """Requirement 6.4 / 11.1: find it whatever the sheet's casing."""
        stub("dcim/racks", {"id": 9, "name": "EXA1-DH1-R01", "site": {"id": 3}})
        lookups.prefetch("dcim.racks")
        assert lookups.get_or_none("dcim.racks", name="exa1-dh1-r01")["id"] == 9
        assert lookups.get_or_none("dcim.racks", name="  EXA1-Dh1-R01 ")["id"] == 9

    @responses.activate
    def test_a_location_is_scoped_to_its_site(self, lookups):
        """Requirement 7.9: the same datahall name under two datacenters is two locations."""
        stub(
            "dcim/locations",
            {"id": 11, "name": "DH1", "slug": "dh1", "site": {"id": 3, "name": "EXA1"}},
            {"id": 12, "name": "DH1", "slug": "dh1", "site": {"id": 4, "name": "EXA2"}},
        )
        lookups.prefetch("dcim.locations")
        assert lookups.get_or_none("dcim.locations", site=3, name="DH1")["id"] == 11
        assert lookups.get_or_none("dcim.locations", site=4, name="DH1")["id"] == 12
        assert lookups.get_or_none("dcim.locations", site=5, name="DH1") is None

    @responses.activate
    def test_a_child_location_is_scoped_to_site_and_parent(self, lookups):
        stub(
            "dcim/locations",
            {
                "id": 11,
                "name": "MMR1",
                "slug": "mmr1",
                "site": {"id": 3},
                "parent": {"id": 8},
            },
            {
                "id": 12,
                "name": "MMR1",
                "slug": "mmr1",
                "site": {"id": 3},
                "parent": {"id": 9},
            },
        )
        lookups.prefetch("dcim.locations")
        assert lookups.get_or_none("dcim.locations", site=3, parent=8, name="MMR1")["id"] == 11
        assert lookups.get_or_none("dcim.locations", site=3, parent=9, slug="mmr1")["id"] == 12

    @responses.activate
    def test_get_matches_preserves_ambiguity(self, lookups):
        stub(
            "dcim/racks",
            {"id": 9, "name": "R01"},
            {"id": 10, "name": "r01"},
        )
        lookups.prefetch("dcim.racks")
        assert {record["id"] for record in lookups.get_matches("dcim.racks", name="R01")} == {
            9,
            10,
        }

    @responses.activate
    def test_a_missing_object_is_none_rather_than_an_error(self, lookups):
        stub("dcim/device-types")
        lookups.prefetch("dcim.device-types")
        assert lookups.get_or_none("dcim.device-types", model="No Such Model") is None


class TestRequestBudget:
    @responses.activate
    def test_repeated_lookups_hit_the_cache_once(self, lookups):
        stub("dcim/racks", {"id": 1, "name": "EXA1-DH1-R01", "site": {"id": 3}})
        lookups.prefetch("dcim.racks")
        for _ in range(50):
            assert lookups.get_or_none("dcim.racks", name="EXA1-DH1-R01") is not None
        assert len(responses.calls) == 1

    @responses.activate
    def test_a_definitive_miss_does_not_re_query(self, lookups):
        """Once an endpoint is prefetched, an absent value is absent — not worth another request."""
        stub("dcim/racks", {"id": 1, "name": "EXA1-DH1-R01", "site": {"id": 3}})
        lookups.prefetch("dcim.racks")
        for _ in range(10):
            assert lookups.get_or_none("dcim.racks", name="EXA1-DH1-R99") is None
        assert len(responses.calls) == 1

    @responses.activate
    def test_the_request_count_stays_bounded_as_the_row_count_grows(self, lookups):
        """Requirement 15.2: no request-per-row, however long the sheet."""
        racks = [
            {"id": index, "name": f"EXA1-DH1-R{index:03d}", "site": {"id": 3}}
            for index in range(1, 2001)
        ]
        responses.add(responses.GET, f"{BASE}/api/dcim/racks/", json=page(*racks))
        lookups.prefetch("dcim.racks")
        for rack in racks:
            assert lookups.get_or_none("dcim.racks", name=rack["name"]) is not None
        assert len(responses.calls) == 1
        assert lookups.count("dcim.racks") == 2000

    @responses.activate
    def test_prefetching_by_name_chunks_long_lists(self, lookups):
        """2,000 names must not become one enormous URL."""
        for _ in range(20):
            responses.add(responses.GET, f"{BASE}/api/dcim/devices/", json=page())
        lookups.prefetch("dcim.devices", names=[f"host-{index:04d}" for index in range(2000)])
        assert len(responses.calls) == 20  # 2000 names / CHUNK_SIZE of 100

    @responses.activate
    def test_an_un_prefetched_lookup_falls_back_to_one_targeted_request(self, lookups):
        stub("dcim/sites", {"id": 3, "name": "EXA1", "slug": "exa1"})
        assert lookups.get_or_none("dcim.sites", slug="exa1")["id"] == 3
        assert len(responses.calls) == 1
        assert "slug__ie=exa1" in responses.calls[0].request.url


class TestApiQuirks:
    @responses.activate
    def test_records_are_re_verified_locally_so_an_ignored_filter_cannot_match(self, lookups):
        """NetBox returns HTTP 200 and *every* object when a filter name is unrecognised.

        The dangerous outcome would be treating an unrelated record as the match. Indexing on the
        natural key locally means an unfiltered response simply fails to contain the wanted key.
        """
        stub(
            "dcim/sites",
            {"id": 1, "name": "OTHER1", "slug": "other1"},
            {"id": 2, "name": "OTHER2", "slug": "other2"},
        )
        assert lookups.get_or_none("dcim.sites", slug="exa1") is None

    @responses.activate
    def test_a_400_from_an_id_filter_reads_as_not_found(self, lookups):
        """Filtering on an id deleted since validation returns 400, not an empty list."""
        responses.add(
            responses.GET,
            f"{BASE}/api/dcim/locations/",
            json={"site_id": ["Select a valid choice. 99 is not one of the available choices."]},
            status=400,
        )
        assert lookups.get_or_none("dcim.locations", site=99, name="DH1") is None

    @responses.activate
    def test_a_real_transport_failure_still_propagates(self, lookups):
        responses.add(responses.GET, f"{BASE}/api/dcim/sites/", json={}, status=403)
        with pytest.raises(netbox_client.NetBoxUnavailable):
            lookups.get_or_none("dcim.sites", slug="exa1")


class TestTerminations:
    @responses.activate
    def test_ports_are_loaded_in_bulk_and_indexed_by_name(self, lookups):
        stub(
            "dcim/interfaces",
            {"id": 21, "name": "swp1", "device": {"id": 5}, "cable": None},
            {"id": 22, "name": "swp2", "device": {"id": 5}, "cable": None},
            {"id": 23, "name": "swp1", "device": {"id": 6}, "cable": None},
        )
        lookups.prefetch_terminations([5, 6], ("interface",))
        assert lookups.terminations_for(5, "interface")["swp1"]["id"] == 21
        assert lookups.terminations_for(6, "interface")["swp1"]["id"] == 23
        assert len(responses.calls) == 1

    @responses.activate
    def test_panel_front_and_rear_ports_are_kept_apart(self, lookups):
        """The same name on both faces is normal, which is why *_port_side exists."""
        stub("dcim/front-ports", {"id": 31, "name": "MPO1", "device": {"id": 7}, "cable": None})
        stub("dcim/rear-ports", {"id": 41, "name": "MPO1", "device": {"id": 7}, "cable": None})
        lookups.prefetch_terminations([7], ("frontport", "rearport"))
        assert lookups.terminations_for(7, "frontport")["mpo1"]["id"] == 31
        assert lookups.terminations_for(7, "rearport")["mpo1"]["id"] == 41

    @responses.activate
    def test_a_port_name_is_matched_case_insensitively(self, lookups):
        stub("dcim/interfaces", {"id": 21, "name": "SWP1", "device": {"id": 5}, "cable": None})
        lookups.prefetch_terminations([5], ("interface",))
        assert "swp1" in lookups.terminations_for(5, "interface")

    @responses.activate
    def test_occupancy_comes_from_the_already_loaded_port_record(self, lookups):
        """Requirement 9.9, at no extra request cost."""
        stub(
            "dcim/interfaces",
            {"id": 21, "name": "swp1", "device": {"id": 5}, "cable": {"id": 99, "label": "LNK-1"}},
            {"id": 22, "name": "swp2", "device": {"id": 5}, "cable": None},
        )
        lookups.prefetch_terminations([5], ("interface",))
        before = len(responses.calls)
        assert lookups.occupied("dcim.interface", 21)["id"] == 99
        assert lookups.occupied("dcim.interface", 22) is None
        assert len(responses.calls) == before

    @responses.activate
    def test_a_cable_referenced_only_by_id_still_reads_as_occupied(self, lookups):
        stub("dcim/rear-ports", {"id": 41, "name": "MPO1", "device": {"id": 7}, "cable": 77})
        lookups.prefetch_terminations([7], ("rearport",))
        assert lookups.occupied("dcim.rearport", 41) == {"id": 77}

    @responses.activate
    def test_a_device_with_no_ports_yields_an_empty_mapping(self, lookups):
        stub("dcim/interfaces")
        lookups.prefetch_terminations([5], ("interface",))
        assert lookups.terminations_for(5, "interface") == {}


class TestChoices:
    @responses.activate
    def test_choices_are_read_from_the_api(self, lookups):
        responses.add(
            responses.OPTIONS,
            f"{BASE}/api/dcim/cables/",
            json={
                "actions": {
                    "POST": {"type": {"choices": [{"value": "smf-os2"}, {"value": "mmf-om4"}]}}
                }
            },
        )
        assert lookups.choices_for("dcim.cables", "type") == frozenset({"smf-os2", "mmf-om4"})
        assert lookups.choices_for("dcim.cables", "type") == frozenset({"smf-os2", "mmf-om4"})
        assert len(responses.calls) == 1
