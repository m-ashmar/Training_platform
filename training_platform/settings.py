"""
Django settings shim for training_platform project.

This file exists for backward compatibility. It imports from settings_local.py
by default. For production, set DJANGO_SETTINGS_MODULE=training_platform.settings_production

The actual configuration lives in:
    - settings_secrets.py  — Zero-trust secrets management utilities
    - settings_base.py     — Shared configuration (no secrets)
    - settings_local.py    — Local development overrides
    - settings_production.py — Production hardening + runtime guards
"""

# Default: import local settings for backward compatibility
from .settings_local import *  # noqa: F401, F403