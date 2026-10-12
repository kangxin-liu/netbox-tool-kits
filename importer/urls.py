# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""URL names follow `importer:<page>` (see steering/structure.md).

Template downloads are named `<page>_template_csv` and `<page>_template_xlsx`: `design.md` lists a
single `<page>_template` name, which cannot address two formats at one URL name.

Only `validate`, `acknowledge`, and `apply` change state, and all three are POST-only — enforced by
the views having no `get`, so a link or a crawler cannot reach them (Requirements 10.17, 14.3).
"""

from django.urls import path

from importer import views
from importer.services import sheets

app_name = "importer"

PAGE_VIEWS = {
    "hierarchy": views.HierarchyView,
    "devices": views.DevicesView,
    "cabling": views.CablingView,
}

SUFFIX_NAMES = {sheets.CSV_SUFFIX: "csv", sheets.XLSX_SUFFIX: "xlsx"}

urlpatterns = [
    path("", views.home, name="home"),
    path("runs/", views.RunListView.as_view(), name="runs"),
    path("runs/<int:pk>/", views.RunDetailView.as_view(), name="run_detail"),
    path("runs/<int:pk>/issues.csv", views.RunIssuesCsvView.as_view(), name="run_issues"),
    path("runs/<int:pk>/report.csv", views.RunResultsCsvView.as_view(), name="run_report"),
    path(
        "runs/<int:pk>/mutations.csv",
        views.RunMutationsCsvView.as_view(),
        name="run_mutations",
    ),
    path(
        "runs/<int:pk>/reconcile/",
        views.ReconcileRunView.as_view(),
        name="run_reconcile",
    ),
    path(
        "runs/<int:pk>/revert/validate/",
        views.RevertPreflightView.as_view(),
        name="revert_validate",
    ),
    path(
        "runs/<int:pk>/revert/<int:attempt_pk>/confirm/",
        views.RevertConfirmView.as_view(),
        name="revert_confirm",
    ),
]

for slug, view in PAGE_VIEWS.items():
    urlpatterns += [
        path(f"{slug}/", view.as_view(), name=slug),
        path(
            f"{slug}/validate/",
            views.ValidateView.as_view(page=slug),
            name=f"{slug}_validate",
        ),
        path(
            f"{slug}/acknowledge/",
            views.AcknowledgeView.as_view(page=slug),
            name=f"{slug}_ack",
        ),
        path(f"{slug}/apply/", views.ApplyView.as_view(page=slug), name=f"{slug}_apply"),
    ]
    for suffix, short in SUFFIX_NAMES.items():
        urlpatterns.append(
            path(
                f"{slug}/template{suffix}",
                views.TemplateDownloadView.as_view(page=slug, suffix=suffix),
                name=f"{slug}_template_{short}",
            )
        )
