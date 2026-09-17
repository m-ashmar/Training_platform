#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys
import logging

logger = logging.getLogger(__name__)


def main():
    """Run administrative tasks."""
    # NEVER in production. `.env` is a developer's file: it used to be baked into the
    # image by `COPY . .` (see .dockerignore) and `load_dotenv` then supplied a value for
    # every variable `fly secrets set` did not, so the release machine ran on a mixture
    # of production secrets and the author's laptop — including a
    # FIREBASE_CREDENTIALS_PATH pointing at a path that exists on neither, which aborted
    # `manage.py migrate` and therefore every deploy.
    settings_module = os.environ.get("DJANGO_SETTINGS_MODULE", "")
    try:
        if not settings_module.endswith("settings_production"):
            from dotenv import load_dotenv
            from pathlib import Path
            load_dotenv(Path(__file__).resolve().parent / '.env')
    except ImportError:
        # Optional side effect: swallowing this silently is what made the
        # surrounding failures invisible in logs. Control flow is unchanged.
        logger.debug('suppressed non-fatal error', exc_info=True)

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "training_platform.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
