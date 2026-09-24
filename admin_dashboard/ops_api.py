"""The operator's phone (`16-agent-operator/OperatorPhone`): what is waiting in the
console, and a sign-in link for the console on a computer. Mounted at `/api/v1/ops/`.

Everything else operators do stays on the web console at `/dj-admin/` (the design's
README). Who may call any of this is who the console's queues let in —
`operators.is_console_operator` — anyone else is 403 `admins_only`.
"""
from django.conf import settings
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from rest_framework import status
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from . import console_login
from .operators import ConsoleOperatorsOnly

#: The admin site's instance namespace (`admin_dashboard.admin.admin_site`).
CONSOLE = "training_admin"


def console_url(request, path):
    """An absolute console url: `OPS_CONSOLE_BASE_URL` when the console is served under
    its own host, else the host this request came to (the console is this server's
    `/dj-admin/`)."""
    base = (getattr(settings, "OPS_CONSOLE_BASE_URL", "") or "").rstrip("/")
    return f"{base}{path}" if base else request.build_absolute_uri(path)


def _queues():
    """(key, count, changelist url name, query string), in the design's order. One COUNT
    each, on columns the queue pages themselves filter on (each is indexed)."""
    from moderation.services import open_report_count
    from payouts.models import OPEN_STATES, Withdrawal
    from subscription.models import TransferReview
    from trainer_verification.models import ApplicationState, VerificationApplication

    return (
        ("transfer_reviews", TransferReview.objects.filter(status="open").count(),
         "subscription_transferreview_changelist", "status__exact=open"),
        ("trainer_verifications",
         VerificationApplication.objects.filter(state=ApplicationState.PENDING).count(),
         "trainer_verification_verificationqueueentry_changelist", ""),
        ("community_reports", open_report_count(),
         "moderation_report_changelist", "status__in=open,reviewing"),
        ("withdrawals", Withdrawal.objects.filter(state__in=OPEN_STATES).count(),
         "payouts_withdrawalqueueentry_changelist", ""),
    )


class QueueCountsView(APIView):
    permission_classes = [IsAuthenticated, ConsoleOperatorsOnly]

    def get(self, request):
        """`GET /api/v1/ops/queue-counts/` — what is waiting on the operator's desk, the
        four rows of `OperatorPhone` (BACKEND_BUGS B-119).

        Needs `Authorization: Bearer <access token>` of a console operator: an active
        staff account that is a superuser or has `user_type` `admin` — exactly who the
        console's queue pages let in. No body, no query parameters. Identical on every
        build.

        200:

            queues       four rows, in the design's order, each
                         `{key, count, console_url}`:
                           transfer_reviews       transfers on the merchant account no
                                                  checkout claimed exactly, still open
                                                  (`TransferReview`, «دفعات بانتظار
                                                  المطابقة»)
                           trainer_verifications  trainer applications in review
                                                  (`pending`)
                           community_reports      reports open or being reviewed (the
                                                  console's own badge count)
                           withdrawals            trainer withdrawals to process:
                                                  `requested`, `approved` or `sent`
                         `console_url` is the absolute url of that queue's page in the
                         web console (the phone shows it; the page itself asks for a
                         console sign-in)
            total        the sum of the four counts
            console_url  the console's front page

        Cost: one COUNT per queue, whatever the queues hold. Counts are live — nothing is
        cached here.

        | status | `code` | when |
        |---|---|---|
        | `401` | `not_authenticated`, `token_not_valid` | no token; an expired or invalid one |
        | `403` | `admins_only` | the caller is not a console operator (a client, trainer, agent, or a staff account that is not an admin) |
        """
        queues = [
            {"key": key, "count": count,
             "console_url": console_url(
                 request, reverse(f"{CONSOLE}:{page}") + (f"?{query}" if query else ""))}
            for key, count, page, query in _queues()]
        return Response({"queues": queues,
                         "total": sum(row["count"] for row in queues),
                         "console_url": console_url(request, reverse(f"{CONSOLE}:index"))})


# ------------------------------------------------------------ the console sign-in link
class ConsoleLoginEnabled(BasePermission):
    """While `OPS_QR_LOGIN_ENABLED` is off: 403 `console_login_disabled` on every route,
    before anything else is asked (a signed-out caller still gets 401 first)."""
    code = "console_login_disabled"
    message = _("Signing in to the console from the phone is not switched on.")

    def has_permission(self, request, view):
        return console_login.enabled()


class _ConsoleLoginBase(APIView):
    permission_classes = [IsAuthenticated, ConsoleLoginEnabled, ConsoleOperatorsOnly]

    @staticmethod
    def refused(refusal):
        body = {"error": refusal.message, "detail": refusal.message, "code": refusal.code,
                **refusal.extra}
        headers = ({"Retry-After": str(refusal.extra["retry_after_seconds"])}
                   if "retry_after_seconds" in refusal.extra else None)
        return Response(body, status=refusal.status, headers=headers)

    @staticmethod
    def payload(ticket):
        """The link as the phone reads it — never the number the computer shows."""
        state = console_login.state_of(ticket)
        opened = None
        if ticket.claimed_at is not None:
            opened = {"opened_at": ticket.claimed_at,
                      "user_agent": ticket.claimed_user_agent}
        return {"id": str(ticket.pk), "state": state, "expires_at": ticket.expires_at,
                "expires_in": console_login.seconds_left(ticket),
                "computer": opened, "approved_at": ticket.approved_at,
                "used_at": ticket.used_at,
                "attempts_left": max(0, console_login.MAX_WRONG_CODES - ticket.wrong_codes)}


class ConsoleLoginView(_ConsoleLoginBase):
    """The operator's phone asks for a link that opens the web console on a computer
    (BACKEND_BUGS B-120; `admin_dashboard/console_login.py` has the whole flow and why).

    **Off by default.** Behind `OPS_QR_LOGIN_ENABLED` (settings), False unless the owner
    switches it on: while off, this route and the three below answer 403
    `console_login_disabled` to a signed-in caller and write nothing.

    **The link alone signs nobody in.** The computer that opens it shows a three-digit
    number; THIS phone must type it (`POST …/{id}/approve/`) within the two minutes;
    only then is the browser that opened the link — no other — signed in, once.
    """

    def post(self, request):
        """`POST /api/v1/ops/console-login/` — a new sign-in link. No body.

        Needs `Authorization: Bearer <access token>` of a console operator whose address
        was verified by the OTP flow. Making a link cancels the caller's earlier one.
        Identical on every build.

        201 `{id, state: "issued", url, expires_at, expires_in: 120, computer: null,
        approved_at: null, used_at: null, attempts_left: 3}` — draw `url` as the QR (and
        offer it to be mailed); it is valid for `expires_in` seconds and for one computer.
        `url` is the only place its secret exists: the server keeps a hash, so a lost
        link is replaced by making a new one. Then watch it with `GET …/{id}/`.

        | status | `code` | when |
        |---|---|---|
        | `401` | `not_authenticated`, `token_not_valid` | no token; an expired or invalid one |
        | `403` | `console_login_disabled` | `OPS_QR_LOGIN_ENABLED` is off |
        | `403` | `admins_only` | the caller is not a console operator |
        | `403` | `email_not_verified` | the caller's address was never verified by the OTP flow |
        | `429` | `throttled` | more than 5 links in 10 minutes; `retry_after_seconds` (and `Retry-After`) |
        """
        if not console_login.may_sign_in(request.user):
            message = _("Verify your email address before signing in to the console.")
            return Response({"error": message, "detail": message,
                             "code": "email_not_verified"},
                            status=status.HTTP_403_FORBIDDEN)
        try:
            ticket, secret = console_login.issue(request.user)
        except console_login.Refusal as refusal:
            return self.refused(refusal)
        url = console_url(request, reverse("ops_qr_login", args=[secret]))
        return Response({**self.payload(ticket), "url": url},
                        status=status.HTTP_201_CREATED)


class ConsoleLoginDetailView(_ConsoleLoginBase):
    def get(self, request, ticket_id):
        """`GET /api/v1/ops/console-login/{id}/` — the phone watches its link (poll
        every 2 seconds while the screen is open; it lives two minutes).

        200 `{id, state, expires_at, expires_in, computer, approved_at, used_at,
        attempts_left}`. `state`:

            issued     no computer has opened it yet
            claimed    a computer opened it: ask the operator for the number it shows,
                       then `POST …/approve/`. `computer` is `{opened_at, user_agent}`
                       of that computer, so the operator can tell it is theirs
            approved   the number was right; the computer signs in on its next check
            used       that computer is signed in. Final
            cancelled  cancelled from the phone, replaced by a newer link, three wrong
                       numbers, or the account lost the console. Final
            expired    the two minutes ran out first. Final

        The number itself is never in this answer.

        | status | `code` | when |
        |---|---|---|
        | `401` | `not_authenticated`, `token_not_valid` | no token; an expired or invalid one |
        | `403` | `console_login_disabled` | `OPS_QR_LOGIN_ENABLED` is off |
        | `403` | `admins_only` | the caller is not a console operator |
        | `404` | `console_login_not_found` | no such link of the caller's (another operator's is not found either) |
        """
        try:
            ticket = console_login.read(ticket_id, request.user)
        except console_login.Refusal as refusal:
            return self.refused(refusal)
        return Response(self.payload(ticket))


class ConsoleLoginApproveView(_ConsoleLoginBase):
    def post(self, request, ticket_id):
        """`POST /api/v1/ops/console-login/{id}/approve/` — the operator types the number
        the computer shows. Body `{"code": "472"}`.

        200 — the link, `state: "approved"`: the computer is signed in on its next check
        (within two seconds).

        | status | `code` | when |
        |---|---|---|
        | `400` | `console_login_code_required` | no `code` (with `field: "code"`) |
        | `400` | `console_login_code_mismatch` | not the number that computer shows; carries `attempts_left` and `state` — at 0 the link is `cancelled` |
        | `401` | `not_authenticated`, `token_not_valid` | no token; an expired or invalid one |
        | `403` | `console_login_disabled` | `OPS_QR_LOGIN_ENABLED` is off |
        | `403` | `admins_only` | the caller is not a console operator |
        | `404` | `console_login_not_found` | no such link of the caller's |
        | `409` | `console_login_not_claimed` | no computer has opened the link yet |
        | `409` | `console_login_closed` | already approved, used or cancelled (with `state`) |
        | `410` | `console_login_expired` | the two minutes ran out |
        """
        body = request.data if isinstance(request.data, dict) else {}
        try:
            ticket = console_login.approve(ticket_id, request.user, body.get("code"))
        except console_login.Refusal as refusal:
            return self.refused(refusal)
        return Response(self.payload(ticket))


class ConsoleLoginCancelView(_ConsoleLoginBase):
    def post(self, request, ticket_id):
        """`POST /api/v1/ops/console-login/{id}/cancel/` — "that is not my computer", or
        the operator left the screen. No body. Idempotent: a link already cancelled, or
        whose two minutes ran out, answers 200 too.

        200 — the link, `state: "cancelled"`.

        | status | `code` | when |
        |---|---|---|
        | `401` | `not_authenticated`, `token_not_valid` | no token; an expired or invalid one |
        | `403` | `console_login_disabled` | `OPS_QR_LOGIN_ENABLED` is off |
        | `403` | `admins_only` | the caller is not a console operator |
        | `404` | `console_login_not_found` | no such link of the caller's |
        | `409` | `console_login_closed` | the computer is already signed in (`state: "used"`): sign out there |
        """
        try:
            ticket = console_login.cancel(ticket_id, request.user)
        except console_login.Refusal as refusal:
            return self.refused(refusal)
        return Response(self.payload(ticket))
