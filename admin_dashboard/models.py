"""The operator console's own records.

`ConsoleLogin` is one sign-in to the web console started from the operator's phone
(`OperatorPhone`, BACKEND_BUGS B-120). The row is also its audit record: who asked, when,
which computer opened the link, when the phone approved it and when that computer was
signed in — or why it was not. See `admin_dashboard/console_login.py` for the flow and
why each step is there.
"""
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class ConsoleLogin(models.Model):
    class State(models.TextChoices):
        #: Shown on the phone as a QR and a link; no computer has opened it.
        ISSUED = "issued", "Issued"
        #: A computer opened the link and shows a number; the phone has not typed it.
        CLAIMED = "claimed", "Opened on a computer"
        #: The phone typed the number that computer shows.
        APPROVED = "approved", "Approved on the phone"
        #: That computer was given a console session. Final.
        USED = "used", "Signed in"
        #: The phone cancelled it, a newer link replaced it, three wrong numbers were
        #: typed, or the operator lost the console before the computer asked. Final.
        CANCELLED = "cancelled", "Cancelled"

    #: The states in which the two minutes are still running.
    LIVE = (State.ISSUED, State.CLAIMED, State.APPROVED)

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="console_logins")
    #: sha256 of the link's secret. The secret itself is only ever in the link.
    token_digest = models.CharField(max_length=64, unique=True)
    state = models.CharField(max_length=10, choices=State.choices, default=State.ISSUED,
                             db_index=True)
    expires_at = models.DateTimeField()
    #: sha256 of the secret the computer that opened the link holds in a cookie: the
    #: session goes to that browser and no other.
    browser_digest = models.CharField(max_length=64, blank=True, default="")
    #: The number that computer shows and the phone must type. Three digits, never
    #: answered by the API.
    match_code = models.CharField(max_length=3, blank=True, default="")
    wrong_codes = models.PositiveSmallIntegerField(default=0)
    #: Which computer opened it, for the operator's own audit.
    claimed_ip = models.GenericIPAddressField(null=True, blank=True)
    claimed_user_agent = models.CharField(max_length=200, blank=True, default="")
    #: `phone`, `replaced`, `wrong_codes` or `not_operator`; empty unless cancelled.
    cancel_reason = models.CharField(max_length=16, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    used_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "Console sign-in from the phone"
        verbose_name_plural = "Console sign-ins from the phone"
        constraints = [
            # One live link per operator: making a new one cancels the old
            # (`console_login.issue`), and the database says so too.
            models.UniqueConstraint(fields=["user"],
                                    condition=Q(state__in=["issued", "claimed",
                                                           "approved"]),
                                    name="one_live_console_login_per_user"),
        ]

    def __str__(self):
        return f"console sign-in {self.pk} ({self.state})"
