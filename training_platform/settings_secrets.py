"""
settings_secrets.py - Zero-trust secrets management utilities.

Provides strict environment parsing and secrets fetching.
Secrets come from AWS Secrets Manager when `AWS_SECRET_NAME` is set, and otherwise from
the process environment — which is where the platform we deploy to (`fly secrets set`)
injects them. Missing secrets crash the application at startup - no defaults, no
fallbacks, whichever store is in use.
"""

import os
import json
import logging

from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)


def require_env(name: str) -> str:
    """Fetch a required environment variable. Crash if missing."""
    value = os.environ.get(name)
    if not value:
        raise ImproperlyConfigured(
            f"Required environment variable '{name}' is not set. "
            f"Application cannot start without it."
        )
    return value


def get_env(name: str, default: str = "") -> str:
    """Fetch an optional environment variable with a safe default."""
    return os.environ.get(name, default)


def get_bool_env(name: str) -> bool:
    """
    Parse a boolean environment variable with STRICT validation.
    Only accepts exactly 'True' or 'False'. Any other value crashes startup.
    """
    val = os.environ.get(name)
    if val is None:
        raise ImproperlyConfigured(
            f"Required boolean environment variable '{name}' is not set."
        )
    if val not in {"True", "False"}:
        raise ImproperlyConfigured(
            f"Environment variable '{name}' must be strictly 'True' or 'False', "
            f"got '{val}'. Ambiguous values are not allowed."
        )
    return val == "True"


def get_bool_env_optional(name: str, default: bool) -> bool:
    """Parse an optional boolean env var. Falls back to default if not set."""
    val = os.environ.get(name)
    if val is None:
        return default
    if val not in {"True", "False"}:
        raise ImproperlyConfigured(
            f"Environment variable '{name}' must be strictly 'True' or 'False', "
            f"got '{val}'."
        )
    return val == "True"


def get_int_env(name: str, default: int) -> int:
    """Parse an integer environment variable."""
    val = os.environ.get(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        raise ImproperlyConfigured(
            f"Environment variable '{name}' must be an integer, got '{val}'."
        )


def get_secret(name: str) -> str:
    """Fetch a highly sensitive secret. Fail-closed: no defaults, ever.

    The secret STORE is whatever the platform actually injects. Set `AWS_SECRET_NAME`
    and the value is read from AWS Secrets Manager; otherwise it comes from the process
    environment, which is where `fly secrets set` puts it.

    This used to demand Secrets Manager in production and ignore the environment
    entirely. `DEPLOY_CHECKLIST.md` documents the deploy as `fly secrets set ...`,
    `fly.toml` provisions no AWS credentials, and the Dockerfile sets none — so the
    documented deploy could not boot: settings raised ImproperlyConfigured("Unable to
    locate credentials") on the first `get_secret` call, before `migrate` ran, and
    `training_platform/celery.py` calls `django.setup()` at import so the worker died the
    same way. The message pointed at infrastructure nobody had provisioned.

    Neither source has a fallback value: a missing secret still crashes startup.
    """
    if os.environ.get("AWS_SECRET_NAME"):
        secret_store_name = os.environ["AWS_SECRET_NAME"]
        
        # Memoize the AWS secrets dict to avoid fetching on every get_secret call
        if not hasattr(get_secret, "_aws_secrets"):
            try:
                import boto3
                region = os.environ.get("AWS_REGION", "us-east-1")
                client = boto3.client("secretsmanager", region_name=region)
                response = client.get_secret_value(SecretId=secret_store_name)
                import json
                get_secret._aws_secrets = json.loads(response["SecretString"])
            except ImportError:
                raise ImproperlyConfigured(
                    "boto3 is required for AWS Secrets Manager. "
                    "Install it with: pip install boto3"
                )
            except Exception as e:
                raise ImproperlyConfigured(
                    f"Failed to fetch secrets from AWS Secrets Manager ({secret_store_name}): {e}"
                )
        
        val = get_secret._aws_secrets.get(name)
        if not val:
            raise ImproperlyConfigured(
                f"Secret '{name}' missing from AWS Secrets Manager '{secret_store_name}'. "
                f"No fallback or default values allowed."
            )
        return val

    return require_env(name)


def get_aws_secret(secret_name: str) -> dict:
    """Fetch a secret from AWS Secrets Manager. Returns parsed JSON dict."""
    try:
        import boto3
        region = os.environ.get("AWS_REGION", "us-east-1")
        client = boto3.client("secretsmanager", region_name=region)
        response = client.get_secret_value(SecretId=secret_name)
        return json.loads(response["SecretString"])
    except ImportError:
        raise ImproperlyConfigured(
            "boto3 is required for AWS Secrets Manager. "
            "Install it with: pip install boto3"
        )
    except Exception as e:
        raise ImproperlyConfigured(
            f"Failed to fetch secret '{secret_name}' from AWS Secrets Manager: {e}"
        )
