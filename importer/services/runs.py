# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Validate an upload, or apply a held plan, and record either as a run.

The views below this are deliberately thin. Everything that decides *whether* something happens is
either in the importer pipeline or in `Verdict`; this module's job is only to sequence the steps and
write down what happened, so that the audit trail cannot be skipped by a code path that forgot to.

Two things are worth knowing before reading on:

* **A validation attempt is a run, even when refused.** Nothing is written to NetBox for a
  blocked file, but the record and its issues are persisted anyway (Requirement 16.11). A run with
  `verdict=blocked` and no results is the audit trail for "someone tried, and the tool said no".
* **Failures here are outcomes, not exceptions.** An unreachable NetBox, a file that cannot be
  parsed, and a plan NetBox has moved underneath all end up as a verdict the operator can read. The
  only thing that escapes this module is a bug.
"""

import logging
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.utils import timezone

from importer.importers import importer_for
from importer.importers.base import Plan, RowOutcome, RunReport
from importer.models import ImportRowResult, ImportRun, ValidationIssueRecord
from importer.models import Verdict as RunVerdict
from importer.services.lookups import Lookups
from importer.services.netbox_client import NetBoxUnavailable
from importer.services.sheets import SheetRejected, parse_upload
from importer.validation import STAGES, Code, Severity, ValidationIssue, Verdict

logger = logging.getLogger("importer.audit")


@dataclass
class ValidationOutcome:
    """One validation attempt: what the operator is told, and what was recorded."""

    run: ImportRun
    verdict: Verdict
    plan: Plan | None = None

    @property
    def applyable(self) -> bool:
        return self.plan is not None and self.verdict.is_applyable()


def target_refusal(client: Any) -> tuple[Code, str] | None:
    """Whether this target may be written to at all, before a file is even looked at.

    The version gate is enforced here as well as in the banner: a disabled button is a courtesy,
    and Requirement 2.7 refuses the import itself rather than the affordance for starting it.
    """
    try:
        status = client.status()
    except NetBoxUnavailable as unavailable:
        return Code.NETBOX_UNREACHABLE, unavailable.operator_message
    if not status.supported:
        return Code.NETBOX_VERSION_UNSUPPORTED, status.reason
    return None


def _stages_through(stage: str) -> tuple[str, ...]:
    """A stage that ran and found problems counts as reached (Requirement 10.4).

    The pipeline records stages the same way — S2 appears in `stages_completed` even when S2 is
    what blocked the file — so a report never claims a stage was skipped when it is the one that
    found the problem.
    """
    return STAGES[: STAGES.index(stage) + 1]


def validate_upload(
    *, importer_name: str, user: Any, upload: Any, client: Any
) -> ValidationOutcome:
    """Parse and validate one upload. Writes nothing to NetBox (Requirement 10.1)."""
    importer = importer_for(importer_name)
    filename = upload.name or "upload"

    refusal = target_refusal(client)
    if refusal is not None:
        # Refused before the file is even read. Validating against a target we will not write to
        # would produce a verdict that reads "ready" and can never be applied.
        code, detail = refusal
        verdict = Verdict(
            file=filename,
            sheet=filename,
            fingerprint="",
            issues=[
                ValidationIssue(
                    severity=Severity.ERROR,
                    code=code,
                    file=filename,
                    sheet=filename,
                    expected="a reachable NetBox in the supported version range",
                    message=f"This file was not validated. {detail}",
                )
            ],
            stages_completed=(),
        )
        run = _record_validation(
            importer_name, user, verdict, client, "", state=RunVerdict.UNAVAILABLE
        )
        return ValidationOutcome(run=run, verdict=verdict)

    version = _version_of(client)

    try:
        sheet = parse_upload(
            importer_name,
            filename=filename,
            stream=upload,
            size=upload.size,
            content_type=getattr(upload, "content_type", "") or "",
            max_upload_mb=settings.MAX_UPLOAD_MB,
        )
    except SheetRejected as rejected:
        # S1 failed, so there is no fingerprint to bind a plan to and nothing to plan. The issues
        # are still the whole story the operator needs.
        verdict = Verdict(
            file=filename,
            sheet=filename,
            fingerprint="",
            issues=list(rejected.issues),
            stages_completed=_stages_through(rejected.stage),
        )
        run = _record_validation(
            importer_name, user, verdict, client, version, state=RunVerdict.BLOCKED
        )
        return ValidationOutcome(run=run, verdict=verdict)

    try:
        plan = importer.validate(sheet, Lookups(client=client))
    except NetBoxUnavailable as unavailable:
        verdict = Verdict(
            file=sheet.file,
            sheet=sheet.sheet,
            fingerprint=sheet.fingerprint,
            issues=[
                *sheet.issues,
                ValidationIssue(
                    severity=Severity.ERROR,
                    code=Code.NETBOX_UNREACHABLE,
                    file=sheet.file,
                    sheet=sheet.sheet,
                    expected="a reachable NetBox",
                    message=(
                        "Validation could not check this file against NetBox, so it cannot be "
                        f"applied. {unavailable.operator_message}"
                    ),
                ),
            ],
            # Only S1 is certain: the read that failed could have been S2's choice lookup.
            stages_completed=("S1",),
        )
        run = _record_validation(
            importer_name, user, verdict, client, version, state=RunVerdict.UNAVAILABLE
        )
        return ValidationOutcome(run=run, verdict=verdict)

    state = RunVerdict.BLOCKED if plan.verdict.errors else RunVerdict.READY
    run = _record_validation(importer_name, user, plan.verdict, client, version, state=state)
    logger.info(
        "validated importer=%s user=%s run=%s file=%s verdict=%s errors=%d warnings=%d",
        importer_name,
        getattr(user, "username", "-"),
        run.pk,
        filename,
        state,
        len(plan.verdict.errors),
        len(plan.verdict.warnings),
    )
    return ValidationOutcome(run=run, verdict=plan.verdict, plan=plan)


def _version_of(client: Any) -> str:
    try:
        return client.status().version
    except NetBoxUnavailable:
        return ""


def _record_validation(
    importer_name: str,
    user: Any,
    verdict: Verdict,
    client: Any,
    version: str,
    *,
    state: str,
) -> ImportRun:
    run = ImportRun.objects.create(
        importer=importer_name,
        user=user,
        filename=verdict.file,
        sheet=verdict.sheet,
        netbox_host=getattr(client, "host", ""),
        netbox_version=version,
        validated_at=verdict.validated_at,
        row_count=verdict.row_count,
        error_count=len(verdict.errors),
        warning_count=len(verdict.warnings),
        skipped_count=verdict.skip_count,
        blocked_count=verdict.blocked_count,
        verdict=state,
        file_fingerprint=verdict.fingerprint,
        stages_completed=",".join(verdict.stages_completed),
    )
    _persist_issues(run, verdict.issues)
    return run


def _persist_issues(run: ImportRun, issues: list[ValidationIssue]) -> None:
    ValidationIssueRecord.objects.bulk_create(
        ValidationIssueRecord(
            run=run,
            severity=str(issue.severity),
            code=str(issue.code),
            file=issue.file,
            sheet=issue.sheet,
            line=issue.line,
            column=issue.column or "",
            value=issue.value,
            expected=issue.expected,
            message=issue.message,
            related_line=issue.related_line,
            related_object=issue.related_object or "",
            related_url=issue.related_url or "",
        )
        for issue in issues
    )


def acknowledge_warnings(run: ImportRun, verdict: Verdict) -> None:
    """Record the operator's acknowledgement on both the live verdict and the run (Req 10.9)."""
    verdict.acknowledge()
    run.warnings_acknowledged = True
    run.save(update_fields=["warnings_acknowledged"])
    logger.info("warnings acknowledged run=%s count=%d", run.pk, len(verdict.warnings))


def apply_plan(*, run: ImportRun, plan: Plan, user: Any, client: Any) -> RunReport:
    """Apply a held plan and record the outcome. The gate itself lives in `Verdict`."""
    importer = importer_for(run.importer)
    username = getattr(user, "username", "")

    try:
        report = importer.apply(plan, Lookups(client=client), username=username)
    except NetBoxUnavailable as unavailable:
        # Raised before the pipeline had a report to write into — the status probe, for instance.
        # Nothing was written, and saying so is more useful than a 500 page.
        report = RunReport(
            importer=run.importer,
            file=plan.sheet.file,
            sheet=plan.sheet.sheet,
            host=getattr(client, "host", ""),
            version=run.netbox_version,
            started_at=timezone.now(),
            finished_at=timezone.now(),
            aborted=True,
            abort_reason=(
                f"Nothing was written: NetBox could not be reached to start the apply. "
                f"{unavailable.operator_message}"
            ),
        )

    _record_apply(run, report)

    if not report.aborted:
        plan.verdict.applied_at = report.finished_at or timezone.now()
    elif report.write_count:
        # Partly applied. Re-applying would resume mid-sequence without re-validating, so the
        # plan is spent: the route from here is the report, then a fresh upload (Req 10.16).
        plan.verdict.applied_at = report.finished_at or timezone.now()

    return report


def _record_apply(run: ImportRun, report: RunReport) -> None:
    if report.aborted:
        state = RunVerdict.STALE if not report.write_count else RunVerdict.ABORTED
    else:
        state = RunVerdict.APPLIED

    run.verdict = state
    # An apply that had nothing to create still ran: every row was already present, and "applied,
    # created nothing" is a different fact from "never applied".
    run.applied_at = report.finished_at if not report.aborted or report.write_count else None
    run.finished_at = report.finished_at or timezone.now()
    run.created_count = report.created_count
    run.skipped_count = report.skipped_count
    run.blocked_count = report.blocked_count
    run.aborted_reason = report.abort_reason[:500]
    run.replayed = report.replayed
    run.endpoints_completed = ",".join(report.endpoints_completed)
    run.save()

    ImportRowResult.objects.bulk_create(
        ImportRowResult(
            run=run,
            line=result.line,
            outcome=str(result.outcome),
            object_type=result.object_kind,
            object_id=result.object_id,
            object_name=result.object_name,
            url=result.url,
            column=result.column,
            message=result.message,
            sequence=result.sequence,
        )
        for result in report.results
    )


def created_rows(run: ImportRun) -> list[ImportRowResult]:
    return list(run.results.filter(outcome=RowOutcome.CREATE))
