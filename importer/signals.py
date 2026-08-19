# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Audit logging for authentication events, and clearing held plans on logout.

Only the actor and the outcome are logged. A failed login's credentials are never written out:
`user_login_failed` hands us the submitted credentials dict, and only the username is taken from
it (Requirement 14.7).

Logging out also drops every validated plan the session was holding (Requirement 1.5). The session
flush alone would make them unreachable, since the run ids live there, but a plan is a description
of intended writes and should not sit in a cache after the operator has left.
"""

import logging

from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.dispatch import receiver

from importer.services import plan_store

audit = logging.getLogger("importer.audit")


@receiver(user_logged_in)
def log_login(sender, request, user, **kwargs) -> None:
    audit.info("login succeeded user=%s superuser=%s", user.get_username(), user.is_superuser)


@receiver(user_logged_out)
def log_logout(sender, request, user, **kwargs) -> None:
    audit.info("logout user=%s", user.get_username() if user else "anonymous")
    if user is None or request is None:
        return
    held = plan_store.held(request.session)
    if held:
        plan_store.discard_all(user.pk, held)
        plan_store.forget_all(request.session)
        audit.info("held plans cleared on logout user=%s count=%d", user.get_username(), len(held))


@receiver(user_login_failed)
def log_login_failure(sender, credentials, request=None, **kwargs) -> None:
    audit.warning("login failed user=%s", (credentials or {}).get("username", "unknown"))
