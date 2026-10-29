# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Task 4 — navigation shell and target banner. Requirements 2.4, 3.1, 3.2, 3.4, 3.5, 14.1."""

import pytest
from django.test import override_settings
from django.urls import reverse

from config import env

PAGES = ["hierarchy", "devices", "cabling"]

pytestmark = pytest.mark.django_db


@pytest.fixture
def signed_in(client, django_user_model):
    user = django_user_model.objects.create_superuser(
        username="operator", password="synthetic-test-password", email="operator@example.com"
    )
    client.force_login(user)
    return client


@pytest.mark.parametrize("page", PAGES)
def test_every_page_links_to_all_three(signed_in, page):
    html = signed_in.get(reverse(f"importer:{page}")).content.decode()
    for other in PAGES:
        assert f'href="{reverse(f"importer:{other}")}"' in html


@pytest.mark.parametrize("page", PAGES)
def test_current_page_is_marked_active(signed_in, page):
    html = signed_in.get(reverse(f"importer:{page}")).content.decode()
    active = [
        line for line in html.splitlines() if 'class="active"' in line and "aria-current" in line
    ]
    assert len(active) == 1
    assert reverse(f"importer:{page}") in active[0]


@pytest.mark.parametrize("page", PAGES)
def test_username_and_logout_are_present(signed_in, page):
    html = signed_in.get(reverse(f"importer:{page}")).content.decode()
    assert "operator" in html
    assert f'action="{reverse("logout")}"' in html


@pytest.mark.parametrize("page", PAGES)
def test_target_host_is_shown_and_token_is_not(signed_in, page):
    from django.conf import settings

    html = signed_in.get(reverse(f"importer:{page}")).content.decode()
    assert settings.NETBOX_HOST in html
    assert settings.NETBOX_TOKEN not in html
    assert "Token " not in html


def test_loopback_development_banner_names_the_target(signed_in):
    target = env.netbox_target("http://localhost:8000/")
    with override_settings(
        NETBOX_TARGET=target,
        NETBOX_HOST=target.host,
        NETBOX_LOOPBACK_DEV=True,
    ):
        html = signed_in.get(reverse("importer:hierarchy")).content.decode()
    assert "Development target" in html
    assert "localhost" in html


def test_unverified_tls_shows_a_persistent_banner(signed_in):
    with override_settings(NETBOX_VERIFY_SSL=False):
        html = signed_in.get(reverse("importer:hierarchy")).content.decode()
    assert "verification is <strong>disabled</strong>" in html


@pytest.mark.parametrize("page", PAGES)
def test_scroll_jumps_are_present_and_start_hidden(signed_in, page):
    """Long verdicts need a way back to either end; the controls stay out of the way until then."""
    html = signed_in.get(reverse(f"importer:{page}")).content.decode()
    assert 'id="scroll-to-bottom" hidden' in html
    assert 'id="scroll-to-top" hidden' in html
    assert 'aria-label="Scroll to bottom"' in html
    assert 'aria-label="Scroll to top"' in html
    assert "border-radius: 50%" in html
    assert 'fill="currentColor"' in html
    assert 'class="scroll-jumps"' in html
    assert "position: fixed" in html
    assert "js/scroll.js" in html


def test_login_page_reveals_no_target(client):
    html = client.get(reverse("login")).content.decode()
    from django.conf import settings

    assert settings.NETBOX_HOST not in html
