"""Email addresses out of log lines.

Sixteen log calls put a user's address into the message ("OTP email sent to ...",
"Failed OTP attempt 3/5 for ..."), and every one of them left the process: into the
rotating files in development, into the platform's log stream in production, and into
Sentry, whose `send_default_pii=False` covers request bodies and user fields but not
the text of a log message (the production-readiness audit, 2026-09-25).

Two layers, because the second one exists to catch what the first one misses:

* `mask_email` at the call site. The log line keeps the user id, which is what support
  needs to find the account, and the domain, which is what diagnosing a delivery
  problem needs ("every gmail.com OTP bounced"). `tests/test_prod_platform.py` scans
  every log call for an address passed without it.
* `redact_emails` in the production log formatter (`training_platform/log_format.py`)
  and in Sentry's `before_send` (`training_platform/observability.py`), over the whole
  rendered message and traceback: an address inside an exception's text — an SMTP
  error quoting the recipient, a validation message — is not a call site anyone wrote.
"""
import re

#: Deliberately loose: a false positive masks something that was not an address, which
#: costs nothing; a false negative ships one. The local part excludes `*`, so a masked
#: address is never matched again and redaction is idempotent.
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")


def mask_email(value) -> str:
    """`someone@example.com` -> `s***@example.com`; anything that is not one -> `***`."""
    text = str(value or "")
    local, at, domain = text.rpartition("@")
    if not at or not local or not domain or any(c.isspace() for c in text):
        return "***"
    return f"{local[0]}***@{domain}"


def redact_emails(text):
    """Every address in `text` masked; anything that is not a string is returned as is."""
    if not isinstance(text, str) or "@" not in text:
        return text
    return _EMAIL.sub(lambda match: mask_email(match.group(0)), text)
