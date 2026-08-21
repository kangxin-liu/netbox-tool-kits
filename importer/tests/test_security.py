# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Task 21 — the security regressions, swept in one place.

Individual suites already assert their own corner of this: `test_netbox_client.py` proves a
classified failure carries no token, `test_shell.py` proves the banner shows the host and not the
credential. This file is the sweep that fails when a *new* surface appears — a new page, a new audit
field, a new error path — and quietly carries something it should not.

Three claims, each tested the way it would actually break:

* **The token crosses one boundary only** (Requirement 14.1). Every page, log record, database row,
  and error path is checked against a sentinel token, including the paths that exist to report
  failure — those are where a credential normally escapes, because an error message is built by
  string formatting and reviewed by nobody.
* **Sheet-derived text is data, never markup** (Requirement 14.4). A cell containing `<script>` is
  quoted back to the operator in an issue, so it reaches the browser by design; it must arrive
  escaped.
* **The deployment posture holds** (Requirements 14.2, 5.7, 12.7): `check --deploy` is clean with
  `DEBUG` false, the credential file is untracked, no tracked file carries a secret, and no fixture
  names anything real.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse
from django.views.debug import SafeExceptionReporterFilter

from importer.models import ImportRun
from importer.services.netbox_client import Failure, NetBoxUnavailable, redact
from importer.tests.conftest import upload_of

pytestmark = pytest.mark.django_db

REPO = Path(settings.BASE_DIR)

# Deliberately token-shaped — 40 hex characters, as NetBox issues — so that a leak through a code
# path that formats "Token <credential>" is caught rather than being invisible in a page of prose.
SENTINEL = "deadbeef" * 5

GOOD_ROW = {
    "location": "Example Region",
    "datacenter": "EXA1",
    "datahall": "DH1",
    "rack": "EXA1-DH1-R01",
    "rack_u_height": "45",
}

PAGES = ["importer:hierarchy", "importer:devices", "importer:cabling", "importer:runs"]

# RFC 2606 reserved names plus the loopback and Django's own test host: everything a fixture is
# allowed to name. A real hostname in a fixture is how production detail arrives in a repository.
RESERVED_HOSTS = frozenset(
    {
        "example.com",
        "example.org",
        "example.net",
        "example.internal",
        "netbox.example.com",
        "netbox.example.internal",
        "importer.example.internal",
        "localhost",
        "127.0.0.1",
        "testserver",
    }
)


def validate(browser, rows, page="hierarchy"):
    posted = browser.post(reverse(f"importer:{page}_validate"), {"sheet": upload_of(page, rows)})
    assert posted.status_code == 302
    return browser.get(reverse(f"importer:{page}"))


def every_stored_value() -> str:
    """Every value this app persisted, flattened, so a new field cannot escape the sweep."""
    chunks: list[str] = []
    for run in ImportRun.objects.all():
        for field in run._meta.fields:
            chunks.append(str(getattr(run, field.name)))
        for related in (run.issues.all(), run.results.all()):
            for record in related:
                chunks.extend(str(getattr(record, f.name)) for f in record._meta.fields)
    return "\n".join(chunks)


# --- 14.1: the token crosses one boundary ----------------------------------------------------


class TestTheTokenStaysPut:
    @pytest.fixture(autouse=True)
    def sentinel_token(self, settings):
        """A distinctive token for the length of each test, so a hit is unambiguous."""
        settings.NETBOX_TOKEN = SENTINEL

    @pytest.mark.parametrize("name", PAGES)
    def test_no_page_carries_it(self, browser, netbox, name):
        assert SENTINEL not in browser.get(reverse(name)).content.decode()

    def test_neither_does_the_report_of_a_run_that_applied(self, browser, netbox, caplog):
        with caplog.at_level("INFO", logger="importer.audit"):
            page = validate(browser, [GOOD_ROW])
            browser.post(
                reverse("importer:hierarchy_apply"),
                {"fingerprint": page.context["verdict"].fingerprint},
            )
        run = ImportRun.objects.latest("pk")
        body = browser.get(reverse("importer:run_detail", args=[run.pk])).content.decode()

        assert SENTINEL not in body
        assert SENTINEL not in caplog.text
        assert SENTINEL not in every_stored_value()
        # Both downloads too: they are built from the same records but by different code.
        issues = browser.get(reverse("importer:run_issues", args=[run.pk]))
        results = browser.get(reverse("importer:run_report", args=[run.pk]))
        assert SENTINEL not in issues.content.decode()
        assert SENTINEL not in results.content.decode()

    def test_an_unreachable_target_reports_the_failure_without_it(self, browser, netbox, caplog):
        """The error paths are the ones that leak, so they are the ones tested hardest."""
        netbox.unavailable_on_read["dcim.regions"] = NetBoxUnavailable(
            Failure.CONNECTION, netbox.host, f"Could not connect using Token {SENTINEL}."
        )
        with caplog.at_level("DEBUG"):
            page = validate(browser, [GOOD_ROW])

        assert SENTINEL not in page.content.decode()
        assert SENTINEL not in caplog.text
        assert SENTINEL not in every_stored_value()

    def test_a_netbox_error_body_that_echoes_it_is_redacted(self):
        """NetBox echoing the request back is out of our control; showing it is not."""
        cleaned = redact(f"POST failed: headers={{'Authorization': 'Token {SENTINEL}'}}")
        assert SENTINEL not in cleaned
        assert "[redacted]" in cleaned

    def test_a_rejected_write_records_the_reason_without_it(self, browser, netbox):
        netbox.reject(
            "dcim.regions",
            lambda payloads: (400, f"invalid request (authenticated with Token {SENTINEL})"),
        )
        page = validate(browser, [GOOD_ROW])
        browser.post(
            reverse("importer:hierarchy_apply"),
            {"fingerprint": page.context["verdict"].fingerprint},
        )

        run = ImportRun.objects.latest("pk")
        body = browser.get(reverse("importer:run_detail", args=[run.pk])).content.decode()
        assert run.aborted_reason, "a rejected write must be recorded, redacted but recorded"
        assert SENTINEL not in every_stored_value()
        assert SENTINEL not in body

    def test_the_debug_error_page_would_redact_it(self):
        """Requirement 14.1 covers error pages, which Django builds from settings itself."""
        safe = SafeExceptionReporterFilter().get_safe_settings()
        assert safe["NETBOX_TOKEN"] != SENTINEL
        assert safe["SECRET_KEY"] != settings.SECRET_KEY

    def test_it_is_never_put_in_a_session_or_a_cookie(self, browser, netbox):
        validate(browser, [GOOD_ROW])
        assert SENTINEL not in str(dict(browser.session))
        assert all(SENTINEL not in str(cookie.value) for cookie in browser.cookies.values())


# --- 14.4: sheet text is data ----------------------------------------------------------------

MARKUP = '<script>alert("xss")</script>'


class TestAutoescaping:
    def test_a_cell_containing_markup_comes_back_escaped(self, browser, netbox):
        """The value is quoted back in an issue by design (Requirement 16.1); it arrives inert."""
        page = validate(browser, [{**GOOD_ROW, "rack": MARKUP}])
        body = page.content.decode()

        assert "<script>alert" not in body
        assert "&lt;script&gt;" in body, "the value must be shown, escaped, not dropped"

    def test_the_stored_report_is_escaped_too(self, browser, netbox):
        validate(browser, [{**GOOD_ROW, "rack": MARKUP}])
        run = ImportRun.objects.latest("pk")
        body = browser.get(reverse("importer:run_detail", args=[run.pk])).content.decode()

        assert "<script>alert" not in body
        assert "&lt;script&gt;" in body

    def test_markup_in_a_name_that_validates_is_escaped_in_the_plan_preview(self, browser, netbox):
        """Not every markup-bearing cell is rejected, and the ones that pass still render inert."""
        page = validate(browser, [{**GOOD_ROW, "location": f"Example {MARKUP} Region"}])
        body = page.content.decode()

        assert "<script>alert" not in body
        assert "&lt;script&gt;" in body

    def test_markup_arriving_from_netbox_is_escaped(self, browser, netbox):
        """A value this tool did not create is no more trusted than one it read from a sheet."""
        netbox.add("dcim.regions", name=f"Example {MARKUP} Region", slug="example-region")
        body = validate(browser, [GOOD_ROW]).content.decode()
        assert "<script>alert" not in body

    def test_no_template_marks_sheet_text_safe(self):
        """`|safe` and `mark_safe` on operator-supplied text would undo all of the above."""
        for template in (REPO / "importer" / "templates").rglob("*.html"):
            body = template.read_text()
            assert "|safe" not in body, f"{template.name} disables escaping"
            assert "autoescape off" not in body, f"{template.name} disables escaping"


# --- 14.2, 5.7, 12.7: the deployment posture -------------------------------------------------


class TestDeploymentPosture:
    def test_manage_py_check_deploy_is_clean(self):
        """Requirement 14.2, with warnings treated as failures — a warning here is the finding."""
        environ = {
            "PATH": "/usr/bin:/bin",
            "DJANGO_SECRET_KEY": "synthetic-deploy-check-key-" + "0123456789" * 3,
            "DJANGO_DEBUG": "false",
            "DJANGO_ALLOWED_HOSTS": "importer.example.internal",
            "DJANGO_CSRF_TRUSTED_ORIGINS": "https://importer.example.internal",
            "DJANGO_SERVED_OVER_TLS": "true",
            "DJANGO_SECURE_SSL_REDIRECT": "true",
            "DJANGO_HSTS_SECONDS": "31536000",
            "NETBOX_URL": "https://netbox.example.internal/",
            "NETBOX_TOKEN": SENTINEL,
        }
        finished = subprocess.run(
            [sys.executable, "manage.py", "check", "--deploy", "--fail-level", "WARNING"],
            cwd=REPO,
            env=environ,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert finished.returncode == 0, finished.stdout + finished.stderr

    def test_debug_is_off_in_the_settings_the_suite_runs_under(self):
        assert settings.DEBUG is False

    def test_the_credential_file_is_ignored_and_untracked(self):
        """Requirement 14.1: the one file that holds a real token never reaches a commit."""
        assert ".env" in (REPO / ".gitignore").read_text().splitlines()
        tracked = subprocess.run(
            ["git", "ls-files", ".env", "db.sqlite3"],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert tracked.stdout.strip() == "", f"tracked: {tracked.stdout}"

    def test_no_tracked_file_carries_a_token_shaped_string(self):
        """A 40-hex run is what a NetBox token looks like; none should be committed."""
        token_shaped = re.compile(r"\b(?![0]{40})[0-9a-f]{40}\b")
        for path in tracked_text_files():
            body = path.read_text(errors="ignore")
            for found in token_shaped.findall(body):
                # The only 40-hex string in the tree should be this file's own sentinel.
                assert found == SENTINEL, f"{path} contains a token-shaped string"

    def test_the_settings_module_holds_no_default_credential(self):
        """Requirement 2.2: a missing token stops startup rather than falling back to one."""
        body = (REPO / "config" / "settings.py").read_text()
        assert 'NETBOX_TOKEN = env.require(ENVIRON, "NETBOX_TOKEN")' in body
        assert "NETBOX_TOKEN = env.optional" not in body


# --- 14.6: every fixture is synthetic --------------------------------------------------------


def tracked_text_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, timeout=60
    )
    skip = {".png", ".jpg", ".xlsx", ".ico", ".woff", ".woff2", ".sqlite3"}
    return [
        REPO / name
        for name in listed.stdout.splitlines()
        if (REPO / name).is_file() and Path(name).suffix not in skip
    ]


class TestFixturesAreSynthetic:
    """Requirement 14.6, and the policy line that no production data reaches a lower environment."""

    def test_no_test_file_names_a_host_outside_the_reserved_names(self):
        url = re.compile(r"https?://([A-Za-z0-9._:-]+)")
        offenders: list[str] = []
        for path in (REPO / "importer" / "tests").glob("*.py"):
            for host in url.findall(path.read_text()):
                bare = host.split(":")[0]
                if bare not in RESERVED_HOSTS and not bare.endswith(".example.com"):
                    offenders.append(f"{path.name}: {host}")
        assert offenders == [], f"non-reserved hosts in fixtures: {offenders}"

    def test_no_test_file_names_an_email_outside_example_com(self):
        email = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+)")
        for path in (REPO / "importer" / "tests").glob("*.py"):
            for domain in email.findall(path.read_text()):
                assert domain.endswith("example.com"), f"{path.name}: {domain}"

    def test_every_fixture_password_is_labelled_synthetic(self):
        password = re.compile(r"password=\"([^\"]+)\"")
        for path in (REPO / "importer" / "tests").glob("*.py"):
            for value in password.findall(path.read_text()):
                assert "synthetic" in value, f"{path.name}: {value}"
