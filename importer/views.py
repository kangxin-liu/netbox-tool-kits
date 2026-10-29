# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Thin controllers: resolve the page, call a service, render.

Nothing here decides whether a file may be applied. That decision belongs to `Verdict`, and the
apply view's job is to ask it and to honour the answer with a 409 (Requirement 10.6) — the disabled
button in the template is a courtesy for the operator, not the control.

Both state-changing endpoints are POST-only and CSRF-protected by Django's middleware
(Requirements 10.17, 14.3), and both follow POST-redirect-GET: validating redirects back to the page
so a refresh cannot re-upload, and applying redirects to the run's report so a refresh cannot
re-apply. The server refuses a second apply regardless (Requirement 10.16); the redirect just means
the operator never has to find that out.
"""

import logging
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views import View
from django.views.generic import TemplateView

from importer import columns, reports
from importer.access import SuperuserRequiredMixin
from importer.importers.base import GUARANTEE, RowOutcome
from importer.models import (
    ImportRun,
    MutationAction,
    MutationOutcome,
    RevertAttempt,
    RevertItemOutcome,
    RevertItemResult,
    RevertStatus,
)
from importer.services import (
    mutations,
    netbox_client,
    plan_store,
    readiness,
    revert,
    runs,
    sheets,
)

logger = logging.getLogger("importer.audit")

# slug -> human title. The nav bar renders in this order.
PAGES: dict[str, str] = {
    "hierarchy": "Site & rack hierarchy",
    "devices": "Devices",
    "cabling": "Cabling",
}

PAGE_BLURBS: dict[str, str] = {
    "hierarchy": (
        "Creates direct child locations and racks under an existing parent location. "
        "The site and parent location must already exist and are never changed."
    ),
    "devices": (
        "Places devices into existing racks by face and rack unit. Racks, device roles, and "
        "device types must already exist in NetBox."
    ),
    "cabling": (
        "Creates cables between devices and patch panels. Both ends and their ports must already "
        "exist in NetBox."
    ),
}

UPLOAD_FIELD = "sheet"


def home(request: HttpRequest) -> HttpResponse:
    return redirect("importer:hierarchy")


class ImporterPageView(SuperuserRequiredMixin, TemplateView):
    """The upload form, and the report for whatever this page last validated."""

    template_name = "importer/page.html"
    page: str = ""

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        context["page"] = self.page
        context["page_title"] = PAGES[self.page]
        context["page_blurb"] = PAGE_BLURBS[self.page]
        context["column_specs"] = columns.columns_for(self.page)
        context["accepted"] = ", ".join(sheets.ACCEPTED_SUFFIXES)
        # Requirement 15.3: the bounds are stated before the upload, not discovered by tripping
        # them. S5 enforces the row count regardless.
        context["max_rows"] = settings.MAX_APPLY_ROWS
        context["max_upload_mb"] = settings.MAX_UPLOAD_MB

        # Read-only probes: the target's status and this importer's prerequisites, both of which
        # create nothing (Requirements 2.4, 2.5, 17.2, 17.4).
        client = netbox_client.build_client()
        connection = readiness.connection_status(client)
        context["connection"] = connection
        context["readiness"] = (
            readiness.readiness_for(self.page, client) if connection.can_import else None
        )

        context.update(self._held_context())
        return context

    def _held_context(self) -> dict[str, Any]:
        """The plan this page is showing, if one is still held (Requirement 10.12)."""
        run_id = plan_store.current(self.request.session, self.page)
        if run_id is None:
            return {}

        held = plan_store.fetch(self.request.user.pk, self.page, run_id)
        if held is None:
            # The plan outlived the store — a restart, or a worker that never saw it. The audit
            # record survives, so point at that rather than pretending the report is gone.
            return {
                "expired_run": ImportRun.objects.filter(pk=run_id, user=self.request.user).first()
            }

        return {
            "held": held,
            "run": ImportRun.objects.filter(pk=held.run_id).first(),
            "verdict": held.verdict,
            "plan": held.plan,
            "issue_report": reports.build_issue_report(held.verdict.issues),
            "refusal": held.verdict.refusal_reason(),
        }


class ValidateView(SuperuserRequiredMixin, View):
    """POST an upload, get a verdict. Writes nothing to NetBox (Requirement 10.1)."""

    page: str = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        upload = request.FILES.get(UPLOAD_FIELD)
        if upload is None:
            messages.error(request, "Choose a .csv or .xlsx file to validate.")
            return redirect(f"importer:{self.page}")

        # A new upload supersedes whatever this page was showing, whether or not the file differs
        # (Requirement 10.13: the previous verdict does not survive a re-upload).
        previous = plan_store.current(request.session, self.page)
        if previous is not None:
            plan_store.discard(request.user.pk, self.page, previous)

        client = netbox_client.build_client()
        try:
            outcome = runs.validate_upload(
                importer_name=self.page,
                user=request.user,
                upload=upload,
                client=client,
            )
        finally:
            # Requirement 5.9: the temporary copy goes away even when parsing raised.
            upload.close()

        plan_store.store(
            request.user.pk,
            plan_store.Held(
                run_id=outcome.run.pk,
                importer=self.page,
                verdict=outcome.verdict,
                plan=outcome.plan,
            ),
        )
        plan_store.remember(request.session, self.page, outcome.run.pk)
        return redirect(f"importer:{self.page}")


class AcknowledgeView(SuperuserRequiredMixin, View):
    """Warnings are acknowledged explicitly, never implicitly (Requirement 10.9)."""

    page: str = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        held, response = _require_held(request, self.page)
        if response is not None:
            return response
        assert held is not None  # narrowed by _require_held

        run = get_object_or_404(ImportRun, pk=held.run_id, user=request.user)
        runs.acknowledge_warnings(run, held.verdict)
        plan_store.store(request.user.pk, held)
        return redirect(f"importer:{self.page}")


class ApplyView(SuperuserRequiredMixin, View):
    """The gate. Its first act is to ask the verdict, and 409 is a normal answer."""

    page: str = ""

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        held, response = _require_held(request, self.page)
        if response is not None:
            return response
        assert held is not None

        # Requirement 10.6: the check comes before anything else, so a forged POST on a blocked plan
        # is refused here rather than reaching the write path.
        refusal = held.verdict.refusal_reason()
        if refusal is not None or held.plan is None:
            return _conflict(
                request,
                refusal or "There is no validated plan to apply. Validate the file again.",
            )

        # Requirement 10.13: the browser says which file it thinks it is applying. A mismatch means
        # the page is stale, and applying the held plan would surprise the operator.
        claimed = request.POST.get("fingerprint", "")
        if claimed != held.verdict.fingerprint:
            return _conflict(
                request,
                "This page is showing a different file from the one now validated. Reload the "
                "page and check the plan before applying.",
            )

        run = get_object_or_404(ImportRun, pk=held.run_id, user=request.user)
        client = netbox_client.build_client()
        refused = runs.target_refusal(client)
        if refused is not None:
            # Requirement 2.7: an out-of-range or unreachable target refuses the write itself.
            return _conflict(request, f"Nothing was applied. {refused[1]}")

        report = runs.apply_plan(run=run, plan=held.plan, user=request.user, client=client)

        plan_store.store(request.user.pk, held)  # carries applied_at, so a second apply is refused
        messages.success(request, report.headline)
        return redirect("importer:run_detail", pk=run.pk)


def _require_held(
    request: HttpRequest, page: str
) -> tuple[plan_store.Held | None, HttpResponse | None]:
    run_id = plan_store.current(request.session, page)
    held = plan_store.fetch(request.user.pk, page, run_id) if run_id is not None else None
    if held is None:
        return None, _conflict(
            request,
            "No validated plan is being held for this page, so there is nothing to apply. "
            "Upload the file and validate it again.",
        )
    return held, None


def _conflict(request: HttpRequest, reason: str) -> HttpResponse:
    """409, because the request was well-formed and the state refuses it (Requirement 10.6)."""
    logger.warning(
        "refused user=%s path=%s reason=%s",
        getattr(request.user, "username", "-"),
        request.path,
        reason,
    )
    return HttpResponse(reason, status=409, content_type="text/plain; charset=utf-8")


CONTENT_TYPES = {
    sheets.CSV_SUFFIX: "text/csv; charset=utf-8",
    sheets.XLSX_SUFFIX: ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
}


class TemplateDownloadView(SuperuserRequiredMixin, View):
    """Generated from `columns.py` on every request, so a template cannot go stale (Req 4.2)."""

    page: str = ""
    suffix: str = sheets.CSV_SUFFIX

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        payload = sheets.build_template(self.page, self.suffix)
        filename = sheets.template_filename(self.page, self.suffix)
        return _download(payload, filename, CONTENT_TYPES[self.suffix])


def _download(payload: bytes, filename: str, content_type: str) -> HttpResponse:
    response = HttpResponse(payload, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


class HierarchyView(ImporterPageView):
    page = "hierarchy"


class DevicesView(ImporterPageView):
    page = "devices"


class CablingView(ImporterPageView):
    page = "cabling"


# --- the audit trail ------------------------------------------------------------------------


class RunListView(SuperuserRequiredMixin, TemplateView):
    """Read-only history, newest first (Requirements 13.4, 13.5)."""

    template_name = "importer/runs.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        history = list(ImportRun.objects.select_related("user").prefetch_related("mutations")[:200])
        reverted_by_run: dict[int, set[int]] = {}
        for run_id, mutation_id in RevertItemResult.objects.filter(
            mutation__run_id__in=[run.pk for run in history],
            outcome=RevertItemOutcome.REVERTED,
        ).values_list("mutation__run_id", "mutation_id"):
            reverted_by_run.setdefault(run_id, set()).add(mutation_id)
        for run in history:
            run.has_mutation_journal = bool(list(run.mutations.all()))
            run.mutation_summary = revert.mutation_summary(
                run, reverted_ids=reverted_by_run.get(run.pk, set())
            )
        context["runs"] = history
        return context


class RunDetailView(SuperuserRequiredMixin, TemplateView):
    template_name = "importer/run_detail.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        run = get_object_or_404(
            ImportRun.objects.select_related("user").prefetch_related(
                "mutations__revert_results__attempt",
                "revert_attempts__items__mutation",
            ),
            pk=kwargs["pk"],
        )
        issues = list(run.issues.all())
        mutation_list = list(run.mutations.all())
        for mutation in mutation_list:
            outcomes = list(mutation.revert_results.all())
            mutation.revert_state = outcomes[-1].outcome if outcomes else ""
            mutation.is_reverted = any(
                outcome.outcome == RevertItemOutcome.REVERTED for outcome in outcomes
            )
            mutation.field_changes = [
                (field, before, mutation.after_values.get(field))
                for field, before in mutation.before_values.items()
            ]
        groups: dict[tuple[str, str], list[Any]] = {}
        for mutation in mutation_list:
            groups.setdefault((mutation.action, mutation.object_type), []).append(mutation)
        context["run"] = run
        context["results"] = list(run.results.all())
        context["created"] = [
            result for result in run.results.all() if result.outcome == RowOutcome.CREATE
        ]
        context["issue_report"] = reports.build_issue_report(issues)
        context["guarantee"] = GUARANTEE
        context["endpoints_completed"] = [
            endpoint for endpoint in run.endpoints_completed.split(",") if endpoint
        ]
        context["mutation_summary"] = revert.mutation_summary(run)
        context["mutation_groups"] = [
            {"action": action, "object_type": object_type, "items": items}
            for (action, object_type), items in groups.items()
        ]
        context["revert_eligibility"] = revert.eligibility(run)
        context["revert_attempts"] = list(run.revert_attempts.all())
        context["needs_reconciliation"] = any(
            item.outcome in (MutationOutcome.INTENT, MutationOutcome.UNKNOWN)
            for item in mutation_list
        )
        return context


class RunIssuesCsvView(SuperuserRequiredMixin, View):
    """The complete issue list, however much the browser truncated (Requirement 16.9, 16.10)."""

    def get(self, request: HttpRequest, pk: int, *args: Any, **kwargs: Any) -> HttpResponse:
        run = get_object_or_404(ImportRun, pk=pk)
        payload = reports.issues_csv(run.issues.all())
        return _download(payload, f"run-{run.pk}-issues.csv", CONTENT_TYPES[sheets.CSV_SUFFIX])


class RunResultsCsvView(SuperuserRequiredMixin, View):
    """The apply report (Requirement 12.6)."""

    def get(self, request: HttpRequest, pk: int, *args: Any, **kwargs: Any) -> HttpResponse:
        run = get_object_or_404(ImportRun, pk=pk)
        if not run.results.exists():
            raise Http404("this run applied nothing, so it has no result report")
        payload = reports.results_csv(run.results.all())
        return _download(payload, f"run-{run.pk}-results.csv", CONTENT_TYPES[sheets.CSV_SUFFIX])


class RunMutationsCsvView(SuperuserRequiredMixin, View):
    def get(self, request: HttpRequest, pk: int, *args: Any, **kwargs: Any) -> HttpResponse:
        run = get_object_or_404(ImportRun, pk=pk)
        if not run.mutations.exists():
            raise Http404("this run has no mutation journal")
        payload = reports.mutations_csv(run.mutations.prefetch_related("revert_results__attempt"))
        return _download(
            payload,
            f"run-{run.pk}-mutations.csv",
            CONTENT_TYPES[sheets.CSV_SUFFIX],
        )


class ReconcileRunView(SuperuserRequiredMixin, View):
    def post(self, request: HttpRequest, pk: int, *args: Any, **kwargs: Any) -> HttpResponse:
        run = get_object_or_404(ImportRun, pk=pk)
        client = netbox_client.build_client()
        unresolved = mutations.reconcile_unknown(run, client)
        if unresolved:
            messages.error(
                request,
                f"{len(unresolved)} operation(s) are still ambiguous; revert remains unavailable.",
            )
        else:
            applied = run.mutations.filter(outcome=MutationOutcome.APPLIED)
            run.created_count = applied.filter(action=MutationAction.CREATE).count()
            run.changed_count = applied.filter(action=MutationAction.UPDATE).count()
            run.verdict = "aborted" if applied.exists() else "stale"
            run.save(update_fields=["created_count", "changed_count", "verdict"])
            messages.success(request, "Unknown write outcomes were reconciled against NetBox.")
        return redirect("importer:run_detail", pk=run.pk)


class RevertPreflightView(SuperuserRequiredMixin, View):
    def post(self, request: HttpRequest, pk: int, *args: Any, **kwargs: Any) -> HttpResponse:
        run = get_object_or_404(ImportRun, pk=pk)
        client = netbox_client.build_revert_client()
        attempt = revert.preflight(run=run, user=request.user, client=client)
        if attempt.status != RevertStatus.READY:
            messages.error(request, attempt.failure_reason)
            return redirect("importer:run_detail", pk=run.pk)
        return render(
            request,
            "importer/revert_confirm.html",
            {
                "run": run,
                "attempt": attempt,
                "items": attempt.items.select_related("mutation"),
                "delete_count": attempt.items.filter(
                    mutation__action=MutationAction.CREATE
                ).count(),
                "restore_count": attempt.items.filter(
                    mutation__action=MutationAction.UPDATE
                ).count(),
            },
        )


class RevertConfirmView(SuperuserRequiredMixin, View):
    def post(
        self, request: HttpRequest, pk: int, attempt_pk: int, *args: Any, **kwargs: Any
    ) -> HttpResponse:
        attempt = get_object_or_404(
            RevertAttempt,
            pk=attempt_pk,
            original_run_id=pk,
        )
        client = netbox_client.build_revert_client()
        revert.execute(attempt=attempt, user=request.user, client=client)
        if attempt.status == RevertStatus.REVERTED:
            messages.success(request, "The recorded changes were reverted.")
        else:
            messages.error(request, attempt.failure_reason or attempt.get_status_display())
        return redirect("importer:run_detail", pk=pk)
