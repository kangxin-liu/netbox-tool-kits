# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Task 3 — local superuser-only access. Requirements 1.1–1.6, 14.7."""

import pytest
from django.contrib.auth import get_user_model
from django.urls import NoReverseMatch, reverse

PAGES = ["importer:hierarchy", "importer:devices", "importer:cabling"]

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_account(django_user_model):
    return django_user_model.objects.create_superuser(
        username="operator", password="synthetic-test-password", email="operator@example.com"
    )


@pytest.fixture
def plain_account(django_user_model):
    return django_user_model.objects.create_user(
        username="bystander", password="synthetic-test-password", email="bystander@example.com"
    )


@pytest.mark.parametrize("name", PAGES)
def test_anonymous_is_redirected_to_login(client, name):
    url = reverse(name)
    response = client.get(url)
    assert response.status_code == 302
    assert response.headers["Location"].startswith(reverse("login"))


@pytest.mark.parametrize("name", PAGES)
def test_anonymous_sees_no_netbox_detail(client, name):
    response = client.get(reverse(name), follow=True)
    assert b"netbox.example.com" not in response.content


@pytest.mark.parametrize("name", PAGES)
def test_authenticated_non_superuser_gets_403(client, plain_account, name):
    client.force_login(plain_account)
    assert client.get(reverse(name)).status_code == 403


@pytest.mark.parametrize("name", PAGES)
def test_superuser_reaches_the_page(client, admin_account, name):
    client.force_login(admin_account)
    assert client.get(reverse(name)).status_code == 200


def test_home_redirects_to_hierarchy(client, admin_account):
    client.force_login(admin_account)
    response = client.get(reverse("importer:home"))
    assert response.status_code == 302
    assert response.headers["Location"] == reverse("importer:hierarchy")


def test_no_accounts_exist_out_of_the_box():
    """Requirement 1.6: nothing seeds a default credential."""
    assert get_user_model().objects.count() == 0


@pytest.mark.parametrize("route", ["password_reset", "password_change", "signup", "register"])
def test_no_self_service_account_routes(route):
    """Requirement 1.4: accounts come from manage.py createsuperuser only."""
    with pytest.raises(NoReverseMatch):
        reverse(route)


def test_logout_clears_the_session(client, admin_account):
    client.force_login(admin_account)
    session_key = client.session.session_key
    client.session["pending_plan"] = {"importer": "hierarchy"}
    response = client.post(reverse("logout"))
    assert response.status_code == 302
    assert client.session.session_key != session_key
    assert "pending_plan" not in client.session


def test_logout_requires_post(client, admin_account):
    client.force_login(admin_account)
    assert client.get(reverse("logout")).status_code == 405


def test_login_success_is_audited(client, admin_account, caplog):
    with caplog.at_level("INFO", logger="importer.audit"):
        client.post(
            reverse("login"), {"username": "operator", "password": "synthetic-test-password"}
        )
    assert "login succeeded user=operator" in caplog.text
    assert "synthetic-test-password" not in caplog.text


def test_login_failure_is_audited_without_the_password(client, admin_account, caplog):
    with caplog.at_level("INFO", logger="importer.audit"):
        client.post(reverse("login"), {"username": "operator", "password": "wrong-password"})
    assert "login failed user=operator" in caplog.text
    assert "wrong-password" not in caplog.text


def test_logout_is_audited(client, admin_account, caplog):
    client.force_login(admin_account)
    with caplog.at_level("INFO", logger="importer.audit"):
        client.post(reverse("logout"))
    assert "logout user=operator" in caplog.text
