# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""The shared pipeline: validate through five stages, then apply a validated plan.

Every importer is the same five stages and the same apply loop; only the per-row rules differ. That
is why this module holds the gate, the ordering, and the write path, and a sheet-specific importer
only answers "what does this row mean" (Requirement 10.2).

Validation (no writes at all, Requirement 10.1):

    S1  file structure          done by `services.sheets.parse_upload` before we get here
    S2  cell format and choices required values, numbers, and choice membership
    S3  consistency in the file duplicates and overlaps between rows of the same sheet
    S4  cross-reference         what exists in NetBox already, and what conflicts (D8)
    S5  feasibility             does the plan fit the configured bounds

A stage whose failure makes later stages meaningless stops the run, and the verdict says which
stages were not reached (Requirement 10.4).

Apply issues **one bulk POST per endpoint** because a NetBox list POST is transactional (decision
D7, measured). Two consequences worth being explicit about:

* The half-applied window is between endpoints, not between rows. For the hierarchy importer that is
  three boundaries however long the sheet is, and for cabling one — interfaces before cables, where
  the sheet creates any (D12). Cabling had none at all until then, which is worth remembering as
  something that was given up rather than something that was never there.
* When NetBox rejects a batch with an error that is *not* aligned to a submitted item, the offending
  row cannot be identified from the response, so the batch is replayed one row at a time
  (Requirement 12.2). The rolled-back batch means the replay starts from a clean slate, but the
  replay itself writes: the rows before the offender are created individually, and the report says
  so rather than pretending the endpoint applied atomically.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from django.conf import settings
from django.utils import timezone

from importer import columns as columns_module
from importer.columns import ColumnSpec
from importer.services.lookups import Lookups, fold
from importer.services.netbox_client import NetBoxUnavailable, NetBoxWriteRejected
from importer.services.sheets import ParsedSheet, Row
from importer.validation import (
    Code,
    IssueCollector,
    RowError,
    Severity,
    ValidationIssue,
    Verdict,
    one_of,
    positive_integer,
    require_value,
)

logger = logging.getLogger("importer.audit")


class RowOutcome(StrEnum):
    """What the plan intends for one spreadsheet row.

    `PARTIAL` deliberately does not exist: a row's creates all live in one transactional batch per
    endpoint, so a row is created, skipped, or blocked — never half done (decision D7).
    """

    CREATE = "create"
    SKIP_EXISTS = "skip-already-exists"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class Ref:
    """A pointer to an object created earlier in the same run.

    A location cannot carry its site's id at plan time when the site does not exist yet, so the
    payload carries a `Ref` and apply resolves it once the parent endpoint has been posted.
    """

    endpoint: str
    key: tuple[Any, ...]


class Registry:
    """Natural key → NetBox id, for objects that exist or have just been created."""

    def __init__(self) -> None:
        self._ids: dict[tuple[str, tuple[Any, ...]], int] = {}

    @staticmethod
    def key(*parts: Any) -> tuple[Any, ...]:
        return tuple(part if isinstance(part, int) else fold(part) for part in parts)

    def remember(self, endpoint: str, key: tuple[Any, ...], object_id: int) -> None:
        self._ids[(endpoint, key)] = int(object_id)

    def id_for(self, endpoint: str, key: tuple[Any, ...]) -> int | None:
        return self._ids.get((endpoint, key))

    def remembered(self, endpoint: str) -> list[tuple[tuple[Any, ...], int]]:
        return [(key, object_id) for (ep, key), object_id in self._ids.items() if ep == endpoint]

    def __contains__(self, item: tuple[str, tuple[Any, ...]]) -> bool:
        return item in self._ids


@dataclass
class CreateAction:
    """One object to create. Payload values may be `Ref`s resolved at apply time."""

    endpoint: str
    payload: dict[str, Any]
    key: tuple[Any, ...]
    label: str
    line: int
    object_kind: str = ""
    method: str = "post"
    """`post` creates; `patch` is the device primary-IP exception of Requirement 8.19."""

    lines: tuple[int, ...] = ()
    """Every spreadsheet line this one object came from, where that is more than one.

    A cable group is several rows describing one cable (Requirement 9.16), and the operator wants a
    report line for each row they wrote (9.23). `line` stays the primary line — the one an error is
    attributed to — and this carries the rest.
    """

    @property
    def all_lines(self) -> tuple[int, ...]:
        return self.lines or (self.line,)

    def resolved_payload(self, registry: Registry) -> dict[str, Any]:
        """Swap every `Ref` in the payload for the id its object was created with.

        Refs are resolved at any depth, not only at the top level, because a cable's terminations
        are a *list of dicts* and one of them may name an interface this run is about to create
        (Requirement 9.32, D12). Resolving only top-level values would have posted the `Ref` object
        itself and let NetBox reject something the tool could see was wrong.
        """
        return {name: self._resolve(value, registry) for name, value in self.payload.items()}

    def _resolve(self, value: Any, registry: Registry) -> Any:
        if isinstance(value, Ref):
            object_id = registry.id_for(value.endpoint, value.key)
            if object_id is None:
                raise PrerequisiteMissing(
                    f"{self.label} needs {value.endpoint} {value.key} to exist first, "
                    f"but it was not created."
                )
            return object_id
        if isinstance(value, dict):
            return {name: self._resolve(item, registry) for name, item in value.items()}
        if isinstance(value, list):
            return [self._resolve(item, registry) for item in value]
        return value


class PrerequisiteMissing(Exception):
    """An ordering bug: a payload referenced something that was never created."""


class ApplyRefused(Exception):
    """The gate said no. The view turns this into a 409 with the reason (Requirement 10.6)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class PlannedRow:
    line: int
    outcome: str = RowOutcome.CREATE
    actions: list[CreateAction] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)
    resolved: dict[str, str] = field(default_factory=dict)
    """What each reference resolved to, for the per-row plan table (Requirement 10.18)."""


@dataclass
class Plan:
    """A validated intention. Applying anything else is the failure mode this guards against."""

    importer: str
    sheet: ParsedSheet
    verdict: Verdict
    rows: list[PlannedRow] = field(default_factory=list)
    registry: Registry = field(default_factory=Registry)
    state: dict[str, str] = field(default_factory=dict)
    """S4's view of NetBox, re-checked immediately before the first write (Requirement 10.14)."""

    def actions_for(self, endpoint: str, *, method: str = "post") -> list[CreateAction]:
        return [
            action
            for row in self.rows
            if row.outcome == RowOutcome.CREATE
            for action in row.actions
            if action.endpoint == endpoint and action.method == method
        ]

    @property
    def create_actions(self) -> list[CreateAction]:
        return [action for row in self.rows for action in row.actions]


@dataclass
class RowResult:
    """What actually happened to one row (Requirement 12.1)."""

    line: int
    outcome: str
    object_kind: str = ""
    object_name: str = ""
    object_id: int | None = None
    url: str = ""
    message: str = ""
    column: str = ""
    sequence: int = 0
    """The order this object was written in. Zero means nothing was written for this row, so a
    manual unwind can walk the non-zero sequences backwards (Requirement 12.4)."""


GUARANTEE = (
    "Each endpoint's creates are submitted as one list and are applied all-or-nothing. The "
    "sequence across endpoints is not atomic: an earlier endpoint can succeed and a later one "
    "fail. Nothing this tool creates can be rolled back by this tool — re-running the same sheet "
    "is safe, because everything that exists is skipped."
)


@dataclass
class RunReport:
    """The outcome of an apply, precise enough to unwind by hand (Requirement 12.4)."""

    importer: str
    file: str
    sheet: str
    host: str
    version: str
    started_at: datetime
    finished_at: datetime | None = None
    results: list[RowResult] = field(default_factory=list)
    endpoints_completed: list[str] = field(default_factory=list)
    endpoints_attempted: list[str] = field(default_factory=list)
    aborted: bool = False
    abort_reason: str = ""
    replayed: bool = False
    guarantee: str = GUARANTEE
    write_count: int = 0
    """Objects written so far, which is also the sequence number of the last one."""

    @property
    def created(self) -> list[RowResult]:
        return [result for result in self.results if result.outcome == RowOutcome.CREATE]

    @property
    def created_count(self) -> int:
        """Objects written, not report lines.

        `sequence` is the write order and is zero when nothing was written for that line, so
        counting non-zero sequences counts objects. The two differ once a cable group gives four
        rows one cable: four lines report a create, one object was written, and the operator is
        told the number that appeared in NetBox.
        """
        return len([result for result in self.created if result.sequence])

    @property
    def skipped_count(self) -> int:
        return len([r for r in self.results if r.outcome == RowOutcome.SKIP_EXISTS])

    @property
    def blocked_count(self) -> int:
        return len([r for r in self.results if r.outcome == RowOutcome.BLOCKED])

    @property
    def headline(self) -> str:
        if self.aborted:
            return (
                f"Aborted — {self.created_count} object(s) created before the abort. "
                f"{self.abort_reason}"
            )
        return f"Applied — {self.created_count} created, {self.skipped_count} already present."

    def to_csv(self) -> bytes:
        """Requirement 12.6: the report downloads as a csv, formula-escaped by `reports`."""
        from importer import reports

        return reports.results_csv(self.results)


class BaseImporter(ABC):
    """Subclasses describe one sheet; the pipeline below is identical for all of them."""

    name: str = ""
    endpoint_order: tuple[str, ...] = ()
    apply_phases: tuple[tuple[str, str], ...] | None = None
    """(endpoint, method) pairs. Defaults to one POST per `endpoint_order` entry.

    The device importer inserts a PATCH of `dcim.devices` after the IP creates (Requirement 8.19).
    """

    choice_sources: ClassVar[dict[str, tuple[str, str]]] = {}
    """Column key → (NetBox endpoint, field) whose choices the value must belong to (Req 7.7)."""

    # --- sheet-specific hooks ---------------------------------------------------------------

    def extra_cell_checks(self, row: Row, lookups: Lookups) -> list[RowError]:
        """S2 checks beyond required / numeric / choices.

        Takes `lookups` because some cell rules depend on what the target instance supports rather
        than on the cell alone — a cable profile named against a NetBox that has no profiles is a
        problem with this row, reported here, not a problem with the file (Requirement 9.14).
        """
        return []

    def normalise(self, rows: list[Row]) -> list[Row]:
        """A last chance to fill in what the sheet left implicit, before S2 judges any cell.

        It runs before S2 because the values it fills in are ones S2 would otherwise reject as
        blank: a cable group states its type once and the other rows of that cable inherit it
        (Requirement 9.18), and "required" has to mean "required of the cable" rather than "typed
        on every row" for that to work.

        Returns new rows rather than editing them, so nothing else in the pipeline has to wonder
        whether the values it is reading are the ones the operator typed.
        """
        return rows

    def group_key(self, row: Row) -> Any:
        """Rows sharing a key are planned together by `plan_group`.

        The default gives every row its own group, so an importer that has never heard of grouping
        behaves exactly as it did when the pipeline planned one row at a time.
        """
        return row.line

    def plan_group(
        self,
        rows: list[Row],
        lookups: Lookups,
        registry: Registry,
        state: dict[str, str],
        collector: IssueCollector,
    ) -> list[PlannedRow]:
        """S4 for one group of rows. Returns one `PlannedRow` per row, in the order given.

        Only the cabling sheet needs this: a breakout is several rows describing one cable, and no
        per-row hook can see the other rows it shares a cable with. Everything else keeps the
        row-at-a-time contract through the default below.
        """
        return [self.plan_row(row, lookups, registry, state, collector) for row in rows]

    def check_consistency(self, rows: list[Row], collector: IssueCollector) -> None:
        """S3: what the rows of this sheet imply about each other.

        Deliberately not abstract: a sheet with no cross-row rules is a legitimate sheet, and
        forcing an empty override would add noise without adding safety.
        """
        return None

    def prefetch(self, rows: list[Row], lookups: Lookups) -> None:
        """Load everything S4 will need, in a bounded number of requests (Requirement 15.2).

        Not abstract for the same reason: an importer may have nothing to resolve.
        """
        return None

    def after_phase(
        self,
        endpoint: str,
        method: str,
        registry: Registry,
        lookups: Lookups,
    ) -> None:
        """Run after one apply phase succeeds. Devices use this to index inherited interfaces."""
        return None

    @abstractmethod
    def plan_row(
        self,
        row: Row,
        lookups: Lookups,
        registry: Registry,
        state: dict[str, str],
        collector: IssueCollector,
    ) -> PlannedRow:
        """S4 for one row: resolve references, compare against what exists, and plan creates."""

    # --- helpers for subclasses -------------------------------------------------------------

    def specs(self) -> dict[str, ColumnSpec]:
        return columns_module.spec_by_key(self.name)

    def spec(self, key: str) -> ColumnSpec:
        return self.specs()[key]

    def choices(self, lookups: Lookups, key: str) -> frozenset[str] | tuple[str, ...]:
        source = self.choice_sources.get(key)
        if source is None:
            return self.spec(key).choices
        endpoint, field_name = source
        from_api = lookups.choices_for(endpoint, field_name)
        # An endpoint that reports no choices for the field (an older or patched NetBox) falls back
        # to the sheet's own list rather than rejecting every value.
        return from_api or self.spec(key).choices

    # --- the pipeline -----------------------------------------------------------------------

    def validate(self, sheet: ParsedSheet, lookups: Lookups) -> Plan:
        """Run S2–S5 and return a plan whose verdict decides whether it may be applied."""
        collector = IssueCollector(file=sheet.file, sheet=sheet.sheet)
        collector.issues.extend(sheet.issues)  # S1's INFO issues, e.g. ignored columns
        stages: list[str] = ["S1"]
        registry = Registry()
        state: dict[str, str] = {}
        planned: list[PlannedRow] = []

        rows = self._stage_cells(self.normalise(sheet.rows), collector, lookups)
        stages.append("S2")
        if collector.errors:
            # A malformed cell makes every later stage's answer meaningless for that row, and
            # cross-row consistency can't be judged on values that aren't valid yet.
            return self._plan_from(sheet, collector, stages, planned, registry, state)

        self.check_consistency(rows, collector)
        stages.append("S3")
        if collector.errors:
            return self._plan_from(sheet, collector, stages, planned, registry, state)

        self.prefetch(rows, lookups)
        for group in self._grouped(rows):
            planned.extend(self.plan_group(group, lookups, registry, state, collector))
        # Groups are planned together but reported in sheet order, so the preview and the report
        # still read down the file the way the operator wrote it (Requirement 8.12, 10.18).
        planned.sort(key=lambda row: row.line)
        stages.append("S4")
        if collector.errors:
            return self._plan_from(sheet, collector, stages, planned, registry, state)

        self._stage_feasibility(planned, collector)
        stages.append("S5")
        return self._plan_from(sheet, collector, stages, planned, registry, state)

    def _stage_cells(
        self, rows: list[Row], collector: IssueCollector, lookups: Lookups
    ) -> list[Row]:
        """S2: required values, numbers, and choice membership, reporting every problem found.

        Choice membership asks NetBox what it accepts rather than trusting the list in `columns.py`
        (Requirement 7.7), so a value NetBox added is never rejected here for being new.
        """
        specs = self.specs()
        for row in rows:
            for spec in specs.values():
                value = row.get(spec.key)
                try:
                    if spec.required:
                        require_value(spec, value)
                    if not str(value).strip():
                        continue
                    if spec.numeric:
                        positive_integer(spec, value)
                    allowed = self.choices(lookups, spec.key)
                    if allowed:
                        one_of(spec, value, allowed)
                except RowError as error:
                    collector.add_row_error(error, row.line)
            for error in self.extra_cell_checks(row, lookups):
                collector.add_row_error(error, row.line)
        return rows

    def _grouped(self, rows: list[Row]) -> list[list[Row]]:
        """Rows bucketed by `group_key`, groups in order of first appearance."""
        groups: dict[Any, list[Row]] = {}
        for row in rows:
            groups.setdefault(self.group_key(row), []).append(row)
        return list(groups.values())

    def _stage_feasibility(self, planned: list[PlannedRow], collector: IssueCollector) -> None:
        """S5: does the plan fit the bounds this deployment is configured for (Requirement 15.3)."""
        creates = sum(len(row.actions) for row in planned)
        limit = settings.MAX_APPLY_ROWS
        if len(planned) > limit:
            collector.add(
                Severity.ERROR,
                Code.ROW_COUNT_OVER_LIMIT,
                value=str(len(planned)),
                expected=f"at most {limit} rows",
                message=(
                    f"This file has {len(planned)} data rows, over the {limit}-row limit for a "
                    f"single apply. Split it into smaller files and import them in order."
                ),
            )
        logger.debug("plan built importer=%s rows=%d creates=%d", self.name, len(planned), creates)

    def _plan_from(
        self,
        sheet: ParsedSheet,
        collector: IssueCollector,
        stages: list[str],
        planned: list[PlannedRow],
        registry: Registry,
        state: dict[str, str],
    ) -> Plan:
        blocking = bool(collector.errors)
        for row in planned:
            if blocking and row.outcome == RowOutcome.CREATE:
                # Requirement 10.7: with any error outstanding, no row is applyable, not even the
                # rows that are individually fine.
                row.outcome = RowOutcome.BLOCKED

        verdict = Verdict(
            file=sheet.file,
            sheet=sheet.sheet,
            fingerprint=sheet.fingerprint,
            issues=list(collector.issues),
            stages_completed=tuple(stages),
            row_count=len(planned),
            create_count=0
            if blocking
            else len([row for row in planned if row.outcome == RowOutcome.CREATE]),
            object_count=0 if blocking else _distinct_objects(planned),
            skip_count=len([row for row in planned if row.outcome == RowOutcome.SKIP_EXISTS]),
            blocked_count=len([row for row in planned if row.outcome == RowOutcome.BLOCKED]),
        )
        return Plan(
            importer=self.name,
            sheet=sheet,
            verdict=verdict,
            rows=planned,
            registry=registry,
            state=state,
        )

    # --- apply ------------------------------------------------------------------------------

    def apply(self, plan: Plan, lookups: Lookups, *, username: str = "") -> RunReport:
        """Write the plan, or refuse. Nothing here decides *what* to write — the plan did that."""
        reason = plan.verdict.refusal_reason()
        if reason is not None:
            raise ApplyRefused(reason)

        client = lookups.client
        status = client.status()
        report = RunReport(
            importer=self.name,
            file=plan.sheet.file,
            sheet=plan.sheet.sheet,
            host=client.host,
            version=status.version,
            started_at=timezone.now(),
        )

        try:
            drift = self._detect_drift(plan, lookups)
        except NetBoxUnavailable as unavailable:
            # NetBox went away between validation and apply. Nothing has been written yet, and
            # nothing will be (Requirement 12.3).
            report.aborted = True
            report.abort_reason = (
                f"NetBox could not be re-checked before writing, so nothing was written. "
                f"{unavailable.operator_message}"
            )
            report.finished_at = timezone.now()
            return report

        if drift:
            # Requirement 10.14: abort before writing anything at all.
            report.aborted = True
            report.abort_reason = (
                "NetBox changed after this file was validated, so the plan is no longer safe to "
                f"apply. Nothing was written. {drift} Validate the file again."
            )
            report.finished_at = timezone.now()
            logger.warning(
                "apply aborted before writing importer=%s user=%s reason=stale",
                self.name,
                username or "-",
            )
            return report

        for row in plan.rows:
            if row.outcome == RowOutcome.SKIP_EXISTS:
                report.results.append(
                    RowResult(
                        line=row.line,
                        outcome=RowOutcome.SKIP_EXISTS,
                        message="Already present in NetBox; nothing to do.",
                        object_name=row.resolved.get("object", ""),
                    )
                )

        phases = self.apply_phases or tuple((endpoint, "post") for endpoint in self.endpoint_order)
        for endpoint, method in phases:
            actions = plan.actions_for(endpoint, method=method)
            if not actions:
                self.after_phase(endpoint, method, plan.registry, lookups)
                continue
            report.endpoints_attempted.append(endpoint)
            try:
                if method == "patch":
                    self._apply_patches(endpoint, actions, plan.registry, report, client)
                else:
                    self._apply_endpoint(endpoint, actions, plan.registry, report, client)
            except NetBoxUnavailable as unavailable:
                # Requirement 12.3: connection loss or auth failure aborts the run.
                report.aborted = True
                report.abort_reason = (
                    f"The run stopped at {endpoint}: {unavailable.operator_message} Objects "
                    f"created before this point are listed below and were not rolled back."
                )
                break
            except BatchRejected as rejected:
                report.aborted = True
                report.abort_reason = (
                    f"NetBox rejected the {endpoint} batch, so that batch was rolled back and the "
                    f"run stopped before the next endpoint. {rejected.detail}"
                )
                break
            report.endpoints_completed.append(endpoint)
            self.after_phase(endpoint, method, plan.registry, lookups)

        report.finished_at = timezone.now()
        logger.info(
            "apply finished importer=%s user=%s created=%d skipped=%d blocked=%d aborted=%s",
            self.name,
            username or "-",
            report.created_count,
            report.skipped_count,
            report.blocked_count,
            report.aborted,
        )
        return report

    def _apply_endpoint(
        self,
        endpoint: str,
        actions: list[CreateAction],
        registry: Registry,
        report: RunReport,
        client: Any,
    ) -> None:
        """One bulk POST for this endpoint (decision D7)."""
        batch = self._dedupe(actions, registry)
        if not batch:
            return
        payloads = [action.resolved_payload(registry) for action in batch]

        try:
            created = client.create(endpoint, payloads)
        except NetBoxWriteRejected as rejected:
            self._attribute_rejection(endpoint, batch, payloads, rejected, registry, report, client)
            raise BatchRejected(self._rejection_detail(rejected)) from None

        self._record_created(endpoint, batch, created, registry, report, client)

    def _apply_patches(
        self,
        endpoint: str,
        actions: list[CreateAction],
        registry: Registry,
        report: RunReport,
        client: Any,
    ) -> None:
        """Field-scoped PATCH. One request per action; not a list POST (Requirement 8.19)."""
        batch = self._dedupe(actions, registry)
        if not batch:
            return
        payloads = [action.resolved_payload(registry) for action in batch]
        try:
            updated = client.patch(endpoint, payloads)
        except NetBoxWriteRejected as rejected:
            detail = self._rejection_detail(rejected)
            for action in batch:
                report.results.append(
                    RowResult(
                        line=action.line,
                        outcome=RowOutcome.BLOCKED,
                        object_kind=action.object_kind or "device primary IP",
                        object_name=action.label,
                        message=f"NetBox rejected this primary-IP PATCH: {detail}",
                    )
                )
            raise BatchRejected(detail) from None

        for action, record in zip(batch, updated, strict=False):
            object_id = record.get("id") or action.payload.get("id")
            if isinstance(object_id, Ref):
                object_id = registry.id_for(object_id.endpoint, object_id.key)
            report.write_count += 1
            url = client.object_url(endpoint, int(object_id)) if object_id else ""
            report.results.append(
                RowResult(
                    line=action.line,
                    outcome=RowOutcome.CREATE,
                    object_kind=action.object_kind or "device primary IP",
                    object_name=action.label,
                    object_id=int(object_id) if object_id else None,
                    url=url,
                    message="Primary IP set.",
                    sequence=report.write_count,
                )
            )

    def _dedupe(self, actions: list[CreateAction], registry: Registry) -> list[CreateAction]:
        """Two rows naming the same new parent create it once (Requirement 7.6)."""
        seen: set[tuple[Any, ...]] = set()
        batch: list[CreateAction] = []
        patching = bool(actions) and actions[0].method == "patch"
        for action in actions:
            if action.key in seen:
                continue
            # A PATCH targets an object that already has an id in the registry; skipping it
            # for that reason would drop the primary-IP write of Requirement 8.19.
            if (
                not patching
                and registry.id_for(action.endpoint, action.key) is not None
            ):
                continue
            seen.add(action.key)
            batch.append(action)
        return batch

    def _record_created(
        self,
        endpoint: str,
        batch: list[CreateAction],
        created: list[dict[str, Any]],
        registry: Registry,
        report: RunReport,
        client: Any,
    ) -> None:
        for action, record in zip(batch, created, strict=False):
            object_id = record.get("id")
            if object_id is not None:
                registry.remember(endpoint, action.key, int(object_id))
            report.write_count += 1
            url = client.object_url(endpoint, object_id) if object_id else ""
            lines = action.all_lines
            for index, line in enumerate(lines):
                report.results.append(
                    RowResult(
                        line=line,
                        outcome=RowOutcome.CREATE,
                        object_kind=action.object_kind or endpoint,
                        object_name=action.label,
                        object_id=object_id,
                        url=url,
                        message="Created."
                        if len(lines) == 1
                        else f"Created — one object for lines {_join(lines)}.",
                        # The write happened once, so only the first line carries its sequence
                        # number. A manual unwind walks the non-zero sequences and finds each
                        # object exactly once (Requirement 12.4).
                        sequence=report.write_count if index == 0 else 0,
                    )
                )

    def _attribute_rejection(
        self,
        endpoint: str,
        batch: list[CreateAction],
        payloads: list[dict[str, Any]],
        rejected: NetBoxWriteRejected,
        registry: Registry,
        report: RunReport,
        client: Any,
    ) -> None:
        """Blame the right row for a rejected batch (Requirement 12.2)."""
        per_item = rejected.per_item
        if per_item is not None and len(per_item) == len(batch):
            for action, errors in zip(batch, per_item, strict=False):
                if not errors:
                    report.results.append(
                        RowResult(
                            line=action.line,
                            outcome=RowOutcome.BLOCKED,
                            object_kind=action.object_kind or endpoint,
                            object_name=action.label,
                            message=(
                                "Not applied: another row in the same batch was rejected, and the "
                                "batch rolled back."
                            ),
                        )
                    )
                    continue
                column, text = _first_field_error(errors)
                report.results.append(
                    RowResult(
                        line=action.line,
                        outcome=RowOutcome.BLOCKED,
                        object_kind=action.object_kind or endpoint,
                        object_name=action.label,
                        column=column,
                        message=f"NetBox rejected this row: {text}",
                    )
                )
            return

        self._replay(endpoint, batch, payloads, rejected, registry, report, client)

    def _replay(
        self,
        endpoint: str,
        batch: list[CreateAction],
        payloads: list[dict[str, Any]],
        rejected: NetBoxWriteRejected,
        registry: Registry,
        report: RunReport,
        client: Any,
    ) -> None:
        """Re-submit the rolled-back batch one row at a time to find the offender (Req 12.2).

        NetBox told us *that* something was wrong, not *which* item — a duplicate position, say.
        Line-level attribution is worth more to the operator than the difference between a
        rolled-back batch and rows created one at a time, but the difference is real: the rows
        before the offender are written by this replay, and `replayed` records that so the report
        can say so.
        """
        report.replayed = True
        logger.warning(
            "replaying rejected batch row by row importer=%s endpoint=%s rows=%d",
            self.name,
            endpoint,
            len(batch),
        )
        for index, (action, payload) in enumerate(zip(batch, payloads, strict=False)):
            try:
                created = client.create(endpoint, payload)
            except NetBoxWriteRejected as single:
                column, text = _first_field_error(single.body)
                report.results.append(
                    RowResult(
                        line=action.line,
                        outcome=RowOutcome.BLOCKED,
                        object_kind=action.object_kind or endpoint,
                        object_name=action.label,
                        column=column,
                        message=f"NetBox rejected this row: {text}",
                    )
                )
                for remaining in batch[index + 1 :]:
                    report.results.append(
                        RowResult(
                            line=remaining.line,
                            outcome=RowOutcome.BLOCKED,
                            object_kind=remaining.object_kind or endpoint,
                            object_name=remaining.label,
                            message="Not attempted: the run stopped at the rejected row above.",
                        )
                    )
                return
            self._record_created(endpoint, [action], created, registry, report, client)

        # Every row succeeded individually, so the batch failure was about the list as a whole.
        report.results.append(
            RowResult(
                line=batch[0].line,
                outcome=RowOutcome.BLOCKED,
                object_kind=endpoint,
                message=(
                    "NetBox rejected the batch but accepted every row on its own. "
                    f"NetBox said: {self._rejection_detail(rejected)}"
                ),
            )
        )

    @staticmethod
    def _rejection_detail(rejected: NetBoxWriteRejected) -> str:
        column, text = _first_field_error(rejected.body)
        return f"{column}: {text}" if column else text

    def _detect_drift(self, plan: Plan, lookups: Lookups) -> str:
        """Re-run S4 and describe anything that moved since validation (Requirement 10.14)."""
        fresh = self.validate(plan.sheet, Lookups(client=lookups.client))
        if fresh.verdict.errors:
            codes = sorted({issue.code.value for issue in fresh.verdict.errors})
            return (
                f"Re-validating immediately before writing found {len(fresh.verdict.errors)} "
                f"error(s) that were not there before ({', '.join(codes)})."
            )

        differences: list[str] = []
        for key, signature in plan.state.items():
            now = fresh.state.get(key)
            if now is None:
                differences.append(f"{key} no longer exists")
            elif now != signature:
                differences.append(f"{key} changed ({signature} → {now})")
        for key in fresh.state:
            if key not in plan.state:
                differences.append(f"{key} has appeared")

        if not differences:
            return ""
        shown = "; ".join(differences[: settings.MAX_ISSUES_DISPLAYED])
        more = len(differences) - settings.MAX_ISSUES_DISPLAYED
        return shown + (f"; and {more} more" if more > 0 else "") + "."


def _join(lines: tuple[int, ...]) -> str:
    return ", ".join(str(line) for line in lines[:-1]) + f" and {lines[-1]}"


def _distinct_objects(planned: list[PlannedRow]) -> int:
    """How many objects the plan will actually create.

    Counting actions would overstate it: twenty rows in one new datahall each carry an action for
    that datahall, and apply creates it once (Requirement 7.6). The operator is told the number that
    will appear in NetBox.
    """
    return len(
        {
            (action.endpoint, action.key)
            for row in planned
            if row.outcome == RowOutcome.CREATE
            for action in row.actions
        }
    )


class BatchRejected(Exception):
    """NetBox refused an endpoint's batch. Already attributed to rows by the caller."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _first_field_error(body: Any) -> tuple[str, str]:
    """The most useful (column, text) pair out of whatever shape NetBox returned."""
    if isinstance(body, dict):
        for column, value in body.items():
            if isinstance(value, list) and value:
                return str(column), "; ".join(str(item) for item in value)
            if value:
                return str(column), str(value)
        return "", "no detail returned"
    if isinstance(body, list):
        for item in body:
            if item:
                return _first_field_error(item)
        return "", "no detail returned"
    return "", str(body) if body else "no detail returned"
