# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Local audit models: what each run submitted, and what came back.

This app never mirrors NetBox inventory. It records the decision and the outcome, which is what a
later "who changed this, and from which file" question actually needs (Requirement 13).

Three fields are deliberately absent: the token, the uploaded file, and any raw cell dump. `value`
holds the single offending cell of an issue, because that is what makes the issue actionable —
storing the row would turn an audit log into a copy of the operator's spreadsheet (Req 13.3).

**A validation attempt is a run.** A blocked file writes nothing to NetBox, but it is still
recorded with its issues, because "why did nothing happen at 14:05" is an audit question too
(Requirement 16.11). `verdict` says which kind of run it was.

Nothing here is editable or deletable through the UI: the views expose list and detail only, and
there is no form, no admin registration, and no delete route (Requirement 13.5).
"""

from django.conf import settings
from django.db import models


class Verdict(models.TextChoices):
    """The state a run ended in. Mirrors the verdict state machine in `design.md`."""

    BLOCKED = "blocked", "Blocked — errors, nothing written"
    READY = "ready", "Ready — validated, not yet applied"
    APPLIED = "applied", "Applied"
    ABORTED = "aborted", "Aborted partway — see the report"
    STALE = "stale", "Refused — NetBox moved after validation"
    EXPIRED = "expired", "Refused — the plan expired"
    UNAVAILABLE = "unavailable", "Refused — NetBox could not be reached"


class ImportRun(models.Model):
    """One validation attempt, and the apply that may follow it (Requirement 13.1)."""

    importer = models.CharField(max_length=16)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    filename = models.CharField(max_length=255)
    sheet = models.CharField(max_length=255, blank=True)
    netbox_host = models.CharField(max_length=255)
    netbox_version = models.CharField(max_length=32, blank=True)

    started_at = models.DateTimeField(auto_now_add=True)
    validated_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    created_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    blocked_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    warning_count = models.PositiveIntegerField(default=0)
    row_count = models.PositiveIntegerField(default=0)

    verdict = models.CharField(max_length=16, choices=Verdict.choices)
    file_fingerprint = models.CharField(max_length=64, blank=True)
    """sha256 of the upload, which is what binds a plan to its file (Requirement 10.13)."""

    stages_completed = models.CharField(max_length=32, blank=True)
    warnings_acknowledged = models.BooleanField(default=False)
    aborted_reason = models.CharField(max_length=500, blank=True)
    replayed = models.BooleanField(default=False)
    """A rejected batch was re-sent row by row, so that endpoint was not applied atomically."""

    endpoints_completed = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ("-started_at", "-id")  # Requirement 13.4: newest first.

    def __str__(self) -> str:
        return f"{self.importer} {self.filename} ({self.verdict})"

    @property
    def was_applied(self) -> bool:
        return self.applied_at is not None

    @property
    def stages(self) -> list[str]:
        return [stage for stage in self.stages_completed.split(",") if stage]


class ImportRowResult(models.Model):
    """What happened to one spreadsheet row, precise enough to unwind by hand (Req 12.4, 13.2)."""

    run = models.ForeignKey(ImportRun, related_name="results", on_delete=models.CASCADE)
    line = models.PositiveIntegerField()
    outcome = models.CharField(max_length=24)
    object_type = models.CharField(max_length=64, blank=True)
    object_id = models.PositiveIntegerField(null=True, blank=True)
    object_name = models.CharField(max_length=255, blank=True)
    url = models.URLField(max_length=500, blank=True)
    """Stored rather than rebuilt: the link must still resolve after NETBOX_URL is repointed."""

    column = models.CharField(max_length=64, blank=True)
    message = models.TextField(blank=True)
    sequence = models.PositiveIntegerField(default=0)
    """Apply order. With this, a manual unwind can go in reverse (Requirement 12.4)."""

    class Meta:
        ordering = ("sequence", "line", "id")

    def __str__(self) -> str:
        return f"line {self.line} {self.outcome}"


class ValidationIssueRecord(models.Model):
    """One issue, persisted whether or not anything was applied (Requirement 16.11)."""

    run = models.ForeignKey(ImportRun, related_name="issues", on_delete=models.CASCADE)
    severity = models.CharField(max_length=8)
    code = models.CharField(max_length=48, db_index=True)
    file = models.CharField(max_length=255, blank=True)
    sheet = models.CharField(max_length=255, blank=True)
    line = models.PositiveIntegerField(null=True, blank=True)
    column = models.CharField(max_length=64, blank=True)
    value = models.TextField(blank=True)
    expected = models.TextField(blank=True)
    message = models.TextField(blank=True)
    related_line = models.PositiveIntegerField(null=True, blank=True)
    related_object = models.CharField(max_length=255, blank=True)
    related_url = models.URLField(max_length=500, blank=True)

    class Meta:
        ordering = ("id",)

    def __str__(self) -> str:
        return f"{self.severity} {self.code} line {self.line}"
