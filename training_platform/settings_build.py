"""
settings_build.py — build-time settings for `collectstatic` ONLY.

`collectstatic` runs during `docker build`, where AWS Secrets Manager and real
secrets are unavailable. Importing settings_production there fails (get_secret →
AWS), which previously left the image with an empty staticfiles/ (masked by
`|| true`). This module injects throwaway placeholders for the few secrets that
settings_base resolves at import, forces local secret resolution, then imports
the shared base config.

NEVER use this module to serve traffic — it contains no real secrets and no DB.
"""
import os

# Make get_secret read os.environ (require_env) instead of hitting AWS.
os.environ.setdefault("LOCAL_PROD_TEST", "True")

# collectstatic must not touch external integrations. Firebase fails closed when
# DEBUG is off and its credential file is absent — which is exactly the situation
# inside `docker build`, where the credential is injected at runtime, not build time.
# Left set, a stray FIREBASE_CREDENTIALS_PATH in the build environment aborts the
# build during collectstatic.
os.environ["FIREBASE_CREDENTIALS_PATH"] = ""

# Minimal placeholders for values settings_base loads via get_secret at import.
for _k, _v in {
    "DJANGO_SECRET_KEY": "build-time-placeholder-not-a-real-secret",
    "JWT_PRIVATE_KEY": "build-time-placeholder",
    "JWT_PUBLIC_KEY": "build-time-placeholder",
}.items():
    os.environ.setdefault(_k, _v)

from .settings_base import *  # noqa: F401,F403

DEBUG = False
ALLOWED_HOSTS = ["*"]  # collectstatic does not serve requests
