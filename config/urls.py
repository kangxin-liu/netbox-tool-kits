# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Root URL configuration.

Only login and logout are wired from `django.contrib.auth`: including `auth.urls` wholesale would
add password-reset routes, and Requirement 1.4 says there is no self-service reset. Accounts are
created with `manage.py createsuperuser`.
"""

from django.contrib.auth.views import LoginView, LogoutView
from django.urls import include, path

urlpatterns = [
    path(
        "login/",
        LoginView.as_view(template_name="importer/login.html", redirect_authenticated_user=True),
        name="login",
    ),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("", include("importer.urls")),
]
