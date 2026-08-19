# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Default port for the development server.

Django's `runserver` defaults to 8000, which is where the local netbox-docker instance this tool
talks to already listens. Starting the app there fails with "address already in use" at best, and at
worst shadows NetBox for anything else on the machine — so `manage.py runserver` with no address
argument serves 8080 instead. An explicit `runserver 9000` or `runserver 0.0.0.0:8080` still wins.
"""

DEFAULT_PORT = "8080"


def apply_default_port(argv: list[str]) -> list[str]:
    """Append `DEFAULT_PORT` to a bare `runserver` invocation, leaving everything else alone."""
    if argv[1:2] != ["runserver"]:
        return argv
    # Anything not starting with `-` after the subcommand is already an address or port.
    if any(not argument.startswith("-") for argument in argv[2:]):
        return argv
    return [*argv, DEFAULT_PORT]
