# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Default-deny access control for every page in this app (Requirements 1.2, 1.3).

Anonymous visitors are redirected to the login page; authenticated non-superusers get 403. The two
cases are deliberately distinguished: a redirect tells an anonymous visitor what to do, while a
signed-in user who is not a superuser has nothing to gain from being sent to a login form they are
already past.
"""

from typing import Any

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse


class SuperuserRequiredMixin:
    """Mix in first, ahead of the view class, so the check runs before any dispatch work."""

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        user = request.user
        if not user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not user.is_superuser:
            raise PermissionDenied(
                "This tool is restricted to superusers. Ask the tool owner for access."
            )
        return super().dispatch(request, *args, **kwargs)  # type: ignore[misc]
