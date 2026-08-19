# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""The development server defaults to 8080 so it cannot collide with NetBox on 8000."""

import pytest

from config.dev_server import DEFAULT_PORT, apply_default_port


def test_the_default_port_is_not_netboxs():
    assert DEFAULT_PORT == "8080"


def test_a_bare_runserver_gets_the_default_port():
    assert apply_default_port(["manage.py", "runserver"]) == ["manage.py", "runserver", "8080"]


@pytest.mark.parametrize("flag", ["--noreload", "--nothreading", "-v2"])
def test_flags_alone_still_get_the_default_port(flag):
    assert apply_default_port(["manage.py", "runserver", flag]) == [
        "manage.py",
        "runserver",
        flag,
        "8080",
    ]


@pytest.mark.parametrize("address", ["9000", "0.0.0.0:8000", "127.0.0.1:9999"])
def test_an_explicit_address_wins(address):
    argv = ["manage.py", "runserver", address]
    assert apply_default_port(argv) == argv


@pytest.mark.parametrize(
    "argv",
    [
        ["manage.py", "migrate"],
        ["manage.py", "check", "--deploy"],
        ["manage.py", "createsuperuser"],
        ["manage.py"],
    ],
)
def test_other_commands_are_untouched(argv):
    assert apply_default_port(argv) == argv
