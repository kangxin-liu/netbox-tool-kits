#!/usr/bin/env python
# Classification: INTERNAL
# AI-assisted (Cursor) — review before merge.
"""Django management entry point."""

import os
import sys

from config.dev_server import apply_default_port


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(apply_default_port(sys.argv))


if __name__ == "__main__":
    main()
