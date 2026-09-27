"""One log record, one line of JSON — the production log format.

Production writes every record to stdout and nowhere else (`settings_production.LOGGING`).
A machine's root filesystem is gone when it stops, so the rotating files under `logs/`
that development keeps were lost with every restart, and the platform's log stream
(`fly logs`, or whatever drain the owner attaches) is the only place a log survives.

One line per record because every log pipeline splits on newlines: a multi-line
traceback printed raw arrives as twenty unrelated events, and the one that says which
request failed is not the one that says why. JSON because any sink parses it without a
custom grammar. The traceback travels inside the record as `exc`, escaped.

Fields: `ts` (UTC, milliseconds), `level`, `logger`, `msg`, `where` (module:line),
`pid`, and `exc` / `stack` when the record carries them. Email addresses are masked
in `msg`, `exc` and `stack` (`training_platform/log_privacy.py`).
"""
import datetime
import json
import logging

from training_platform.log_privacy import redact_emails


class OneLineJsonFormatter(logging.Formatter):
    """`logging.Formatter` that renders each record as a single JSON object."""

    def format(self, record):
        try:
            message = record.getMessage()
        except Exception:                      # a bad %-format must not lose the record
            message = f"{record.msg!r} % {record.args!r}"
        payload = {
            "ts": datetime.datetime.fromtimestamp(record.created, tz=datetime.timezone.utc)
                  .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact_emails(message),
            "where": f"{record.module}:{record.lineno}",
            "pid": record.process,
        }
        if record.exc_info:
            payload["exc"] = redact_emails(self.formatException(record.exc_info))
        elif record.exc_text:
            payload["exc"] = redact_emails(record.exc_text)
        if record.stack_info:
            payload["stack"] = redact_emails(self.formatStack(record.stack_info))
        line = json.dumps(payload, ensure_ascii=False, default=str)
        # `ensure_ascii=False` keeps Arabic readable, and leaves the two Unicode line
        # separators raw, which some viewers break a line on.
        return line.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
