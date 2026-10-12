# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Task 2 / 9a — environment configuration refuses to start on a bad target.

Requirements 2.1, 2.2, 2.3, 2.3a, 2.3b, 14.1, 14.2, 14.5.
"""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from config import env

REPO_ROOT = Path(__file__).resolve().parents[2]


class TestRequire:
    def test_names_the_missing_variable(self):
        with pytest.raises(ImproperlyConfigured, match="NETBOX_TOKEN"):
            env.require({}, "NETBOX_TOKEN")

    def test_blank_counts_as_missing(self):
        with pytest.raises(ImproperlyConfigured, match="DJANGO_SECRET_KEY"):
            env.require({"DJANGO_SECRET_KEY": "   "}, "DJANGO_SECRET_KEY")

    def test_returns_trimmed_value(self):
        assert env.require({"NETBOX_URL": " https://nb.example.com/ "}, "NETBOX_URL") == (
            "https://nb.example.com/"
        )


class TestCoercion:
    @pytest.mark.parametrize("raw,expected", [("true", True), ("FALSE", False), ("1", True)])
    def test_flag_accepts_common_spellings(self, raw, expected):
        assert env.flag({"F": raw}, "F", default=not expected) is expected

    def test_flag_rejects_nonsense_rather_than_guessing(self):
        with pytest.raises(ImproperlyConfigured, match="boolean"):
            env.flag({"F": "maybe"}, "F", default=True)

    def test_integer_rejects_non_numeric(self):
        with pytest.raises(ImproperlyConfigured, match="integer"):
            env.integer({"MAX_UPLOAD_MB": "five"}, "MAX_UPLOAD_MB", default=5)

    def test_integer_enforces_minimum(self):
        with pytest.raises(ImproperlyConfigured, match=">= 1"):
            env.integer({"MAX_UPLOAD_MB": "0"}, "MAX_UPLOAD_MB", default=5)

    def test_csv_list_trims_and_drops_blanks(self):
        assert env.csv_list({"H": "a, b ,,c"}, "H") == ["a", "b", "c"]


class TestNetBoxTarget:
    def test_https_remote_is_accepted(self):
        target = env.netbox_target("https://netbox.example.com/")
        assert (target.host, target.scheme, target.loopback_dev) == (
            "netbox.example.com",
            "https",
            False,
        )

    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
    def test_http_loopback_is_the_one_exception(self, host):
        target = env.netbox_target(f"http://{host}:8000/")
        assert target.loopback_dev is True

    def test_http_to_a_remote_host_refuses_to_start(self):
        with pytest.raises(ImproperlyConfigured, match="Plaintext to a remote NetBox"):
            env.netbox_target("http://netbox.example.com/")

    def test_a_loopback_looking_path_does_not_widen_the_exception(self):
        # The host is what matters, not the rest of the URL.
        with pytest.raises(ImproperlyConfigured, match="Plaintext to a remote NetBox"):
            env.netbox_target("http://netbox.example.com/localhost/")

    @pytest.mark.parametrize("url", ["netbox.example.com", "ftp://netbox.example.com/", "https://"])
    def test_a_url_that_is_not_absolute_http_is_refused(self, url):
        with pytest.raises(ImproperlyConfigured, match="absolute http"):
            env.netbox_target(url)


class TestTransportWarnings:
    def test_disabled_verification_warns_at_startup(self, caplog):
        target = env.netbox_target("https://netbox.example.com/")
        with caplog.at_level("WARNING", logger="importer.config"):
            env.warn_about_transport(target, verify_ssl=False)
        assert "NETBOX_VERIFY_SSL is false" in caplog.text

    def test_loopback_development_case_is_logged(self, caplog):
        target = env.netbox_target("http://localhost:8000/")
        with caplog.at_level("WARNING", logger="importer.config"):
            env.warn_about_transport(target, verify_ssl=True)
        assert "loopback development exception" in caplog.text

    def test_warnings_never_include_a_token(self, caplog):
        target = env.netbox_target("http://localhost:8000/")
        with caplog.at_level("WARNING", logger="importer.config"):
            env.warn_about_transport(target, verify_ssl=False)
        assert "Token" not in caplog.text


class TestNoSecretsInSource:
    """Requirement 14.1: no default secret key or token anywhere in tracked source."""

    def test_settings_has_no_fallback_secret_key(self):
        source = (REPO_ROOT / "config" / "settings.py").read_text()
        assert 'SECRET_KEY = env.require(ENVIRON, "DJANGO_SECRET_KEY")' in source
        assert not re.search(r'SECRET_KEY\s*=\s*["\'](?!\s*$)', source)

    def test_the_token_is_read_once_and_never_reassigned(self):
        source = (REPO_ROOT / "config" / "settings.py").read_text()
        statements = [line for line in source.splitlines() if line.startswith("NETBOX_TOKEN")]
        assert statements == ['NETBOX_TOKEN = env.require(ENVIRON, "NETBOX_TOKEN")']

    def test_revert_token_defaults_to_the_forward_token(self):
        assert settings.NETBOX_REVERT_TOKEN == settings.NETBOX_TOKEN

    def test_no_template_can_reach_the_token(self):
        templates = (REPO_ROOT / "importer" / "templates").rglob("*.html")
        offenders = [
            path.name
            for path in templates
            if "NETBOX_TOKEN" in path.read_text() or "netbox_token" in path.read_text()
        ]
        assert offenders == []
