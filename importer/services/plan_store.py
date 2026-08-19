# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Where a validated plan lives between validating it and applying it.

Requirement 10.12 is the reason this module exists: apply must write *the plan that was validated*,
never a file supplied with the apply request. So the plan is held server-side and the browser gets
only a run id, which means a crafted POST can name a plan but cannot describe one.

**Why the cache and not the session.** Django serialises sessions as JSON, and a plan is a graph
of dataclasses holding `Ref`s and payloads. Round-tripping that through JSON would mean writing a
codec whose bugs would be silent and whose failure mode is applying something subtly different from
what was validated. The cache stores the object itself.

**Consequence worth stating.** The default cache backend is per-process, so with more than one
worker an apply can land on a process that has never seen the plan. That fails safe — the plan is
reported as missing and the operator re-validates, nothing written — but it is an annoyance, so a
multi-worker deployment should configure a shared cache backend. Iteration 1 assumes a single
operator and a single process (assumption 3).

Keys are namespaced by user id, and the apply view separately checks that the run belongs to the
requesting user, so one operator cannot reach another's plan by guessing a run id.
"""

from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.cache import cache

from importer.importers.base import Plan
from importer.validation import Verdict

PREFIX = "importer.plan"

# A little longer than the verdict's own lifetime, so an expired plan can be *refused as expired*
# rather than silently vanishing — "validate again, it expired at 14:05" beats "no plan found".
GRACE_MINUTES = 15


@dataclass
class Held:
    """What one page is currently showing.

    `plan` is None when validation never got far enough to plan anything — an unreadable file, or
    a NetBox that could not be reached. The verdict still explains why, which is what the page
    renders, so the three outcomes need only one shape between them.
    """

    run_id: int
    importer: str
    verdict: Verdict
    plan: Plan | None = None

    @property
    def applyable(self) -> bool:
        return self.plan is not None and self.verdict.is_applyable()


def _key(user_id: int, importer: str, run_id: int) -> str:
    return f"{PREFIX}:{user_id}:{importer}:{run_id}"


def timeout_seconds() -> int:
    return (settings.PLAN_EXPIRY_MINUTES + GRACE_MINUTES) * 60


def store(user_id: int, held: Held) -> None:
    cache.set(_key(user_id, held.importer, held.run_id), held, timeout=timeout_seconds())


def fetch(user_id: int, importer: str, run_id: int) -> Held | None:
    return cache.get(_key(user_id, importer, run_id))


def discard(user_id: int, importer: str, run_id: int) -> None:
    cache.delete(_key(user_id, importer, run_id))


def discard_all(user_id: int, held: dict[str, int]) -> None:
    """Drop every plan named in a session's map, which is how logout clears them (Req 1.5)."""
    for importer, run_id in held.items():
        discard(user_id, importer, int(run_id))


# --- the session half: which run each page is currently showing ------------------------------

SESSION_KEY = "importer_plans"
"""importer name → run id. The only plan identifier the browser ever holds (Requirement 10.12)."""


def held(session: Any) -> dict[str, int]:
    raw = session.get(SESSION_KEY) or {}
    return {str(name): int(run_id) for name, run_id in raw.items()}


def remember(session: Any, importer: str, run_id: int) -> None:
    """Point a page at its latest run, so the report survives navigating away (Req 10.12)."""
    current = held(session)
    current[importer] = int(run_id)
    session[SESSION_KEY] = current


def current(session: Any, importer: str) -> int | None:
    return held(session).get(importer)


def forget(session: Any, importer: str) -> None:
    current_map = held(session)
    current_map.pop(importer, None)
    session[SESSION_KEY] = current_map


def forget_all(session: Any) -> None:
    session[SESSION_KEY] = {}
