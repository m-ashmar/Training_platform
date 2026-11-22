"""`/api/v1/coaching/`: the signed-in client's monthly coaching. See users/coaching.py."""
from django.utils.translation import gettext as _
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .coaching import coaching_status


class CurrentCoachingView(APIView):
    """GET /api/v1/coaching/current/ — the client's coaching with their trainer, by month.

    Clients only (403 `clients_only`; 401 without a token). Headers: `X-App-Channel`,
    `X-App-Version` (the price only).

    200 `{"coaching": null}` without an approved trainer, otherwise::

        {"coaching": {
            "trainer_id": 12,
            "since": "2026-08-02T10:00:00Z",       when the trainer accepted
            "status": "awaiting_first_program"     month 1 is held; nothing is paid out
                      | "active"                   a paid month is running (or renewing
                                                   within the hour; or coaching sold as
                                                   one fee before months existed)
                      | "frozen"                   the renewal found the wallet short:
                                                   paused. Routines stay readable and
                                                   cannot be started (403
                                                   `coaching_frozen` on every training
                                                   write) until a month is paid
                      | "unpaid",                  support returned month 1; nothing
                                                   renews until support resolves it
            "period": {"number", "starts_at", "ends_at"} | null,   the latest paid month
            "renews_at": datetime | null,          when the next month is charged
            "renewal_reminder": bool,              the month ends within 3 days: show the
                                                   renewal reminder (every client, the
                                                   owner's rule; a push goes out too)
            "frozen_since": datetime | null,       frozen only: when the paid month ended
            "grace_until": null,                   always null since the freeze (kept so
                                                   the shape does not change)
            "ending": bool,                        the trainer stops when this month ends
                                                   (`period.ends_at`); nothing renews
            "stop_renewal": bool,                  the CLIENT asked not to renew after
                                                   this month (S2.7, «لا تجدّد بعد هذا
                                                   الشهر»); switched with
                                                   `POST coaching/renewal/`
            "ends_at": datetime | null,            when the coaching ends instead of
                                                   renewing (`ending` or `stop_renewal`;
                                                   `renews_at` is then null); null
                                                   while it renews, and before month 1
                                                   has started
            "ending_reminder": bool,               `ends_at` is within the same 3 days
                                                   as `renewal_reminder` (which stays
                                                   false when nothing renews)
            "coaching_price", "coaching_currency",
            "money_type"}}                         ONLY on a build that may show a
                                                   coaching fee (not `ios` today).
                                                   The price is ALWAYS in the client's
                                                   own currency, which is what they are
                                                   charged in; `coaching_currency` is
                                                   that currency, so the app never
                                                   hard-codes a unit. Null together
                                                   when the rate needed to quote it is
                                                   missing or stale.

    `payment_due` is no longer sent: an unpaid month freezes instead of ending after a
    grace (the owner, 2026-09-19). A frozen coaching is charged by the hourly run as soon
    as the wallet covers the month, and that month starts when it is paid. After a
    coaching ends (`{"coaching": null}`) the trainer's routines stay readable and answer
    403 `coaching_ended` to training writes; while the status is `unpaid` they answer
    403 `coaching_unpaid`. Every such 403 carries `trainer_id` and no price.

    Each month is charged from the wallet automatically when the last one ends; the
    platform's commission is taken from every month and is not shown to the client.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .models import TrainerClientRelation

        client = request.user
        if not client.is_client:
            raise PermissionDenied(_('Only clients have coaching.'), code='clients_only')
        relation = (TrainerClientRelation.objects.select_related('trainer')
                    .filter(client=client, status='approved').first())
        if relation is None:
            return Response({'coaching': None})
        return Response({'coaching': coaching_body(request, relation)})


def coaching_body(request, relation) -> dict:
    """`coaching/current/`'s `coaching` object for `relation`: the status, and the price
    on a build that may show a coaching fee."""
    from policy.money import MoneyType
    from policy.services import can_show_price

    from .trainer_relations import charge_for_client

    body = coaching_status(relation)
    if can_show_price(request, MoneyType.COACHING_1TO1):
        # What the next month costs, IN THE CLIENT'S OWN CURRENCY — which is what
        # they will be charged in (the owner, 2026-09-21: a person pays in the one
        # currency they trade in, whatever the trainer prices in).
        #
        # A month that has already been charged says so itself: `CoachingPeriod`
        # carries the price AND its unit, copied off the money that actually moved,
        # so a later rate change cannot re-quote a coaching already running. Before
        # the first month there is nothing charged yet, so the trainer's price of
        # record is converted at today's rate — and if that conversion cannot be
        # priced (no rate, or a stale one) the price is omitted rather than shown
        # in the trainer's currency, which the client would read as their own.
        latest = relation.periods.order_by('-number').first()
        if latest is not None:
            body['coaching_price'] = str(latest.price)
            body['coaching_currency'] = latest.currency
            body['money_type'] = MoneyType.COACHING_1TO1.value
        else:
            from wallet.exchange import ExchangeRateUnavailable

            try:
                amount, currency, _rate = charge_for_client(relation)
            except ExchangeRateUnavailable:
                body['coaching_price'] = body['coaching_currency'] = None
            else:
                body['coaching_price'] = str(amount)
                body['coaching_currency'] = currency
            body['money_type'] = MoneyType.COACHING_1TO1.value
    return body


def _renew_value(raw):
    """The body's `renew` as a bool, or None when it is missing or not a boolean."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().lower() in ('true', 'false'):
        return raw.strip().lower() == 'true'
    return None


class CoachingRenewalView(APIView):
    """POST /api/v1/coaching/renewal/ — the client's «لا تجدّد بعد هذا الشهر» (Road to 10
    S2.7, the owner's D12), switched on and back off.

    Clients only (403 `clients_only`; 401 without a token). Body: `{"renew": false}` —
    do not renew after this month — or `{"renew": true}` to withdraw that (a JSON
    boolean, or the strings `"true"` / `"false"`).

    With `renew: false`, when the paid month ends the coaching ENDS instead of renewing:
    nothing is charged and nothing is held, and the month already paid stays the
    trainer's (no refund — it was delivered). Both people are told it ended
    (`coaching_finished`), and the trainer's routines then stay readable and answer 403
    `coaching_ended` to training writes, as after any ending. The 3-day reminder says it
    ends (push `coaching_ending_soon`) instead of that it renews
    (`coaching_renewal_soon`). Asking before month 1 has started (`awaiting_first_program`)
    is allowed: month 1 then runs and ends. On a `frozen` coaching it is allowed too —
    a frozen coaching renews the moment the wallet covers the month — and the hourly
    run then ends it at once, nothing charged, even if the wallet was topped up
    meanwhile. The trainer sees the choice on the client's file
    (`GET /api/auth/trainer/client-profile/{id}/` `coaching.stop_renewal`) and still
    cannot end the paid month early.

    Repeating either value is harmless: 200, nothing written (a repeated `renew: true`
    still needs a build that may pay for coaching, below). The choice can be switched
    back until the renewal run reaches the month's end; after the coaching has ended,
    asking again starts a new coaching. A trainer's own end (`ending`) is not changed by
    this route.

    200 `{"coaching": {...}}` — exactly the object `GET coaching/current/` answers
    (`stop_renewal`, `ends_at`, `ending_reminder`, `renews_at`, and the price on a build
    that may show a coaching fee).

    What differs by build: `renew: false` is accepted on every build, `ios` included —
    stopping a charge is never a purchase. `renew: true` agrees to the monthly charge
    again, so it is refused where the build may not pay for coaching
    (`require_purchase_allowed(COACHING_1TO1)`): 403 `not_available_on_channel` (today
    the first `ios` build), 400 `app_channel_required` with no `X-App-Channel`.

    | status | `code` | when |
    |---|---|---|
    | `400` | `renew_required` | `renew` is missing or not a boolean (with `field`) |
    | `400` | `app_channel_required` | `renew: true` with no `X-App-Channel` |
    | `403` | `clients_only` | the caller is not a client |
    | `403` | `not_available_on_channel` | `renew: true` on a build that may not pay for coaching |
    | `404` | `no_coaching` | the client has no approved trainer |
    | `409` | `nothing_to_renew` | `renew: false` on a coaching that never renews: free (nothing held, no month), sold as one fee before months existed, or `unpaid` (support returned month 1) |
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        from policy.money import MoneyType
        from policy.services import require_purchase_allowed
        from rest_framework import status as http

        from .coaching import set_renewal

        client = request.user
        if not client.is_client:
            raise PermissionDenied(_('Only clients have coaching.'), code='clients_only')
        renew = _renew_value(request.data.get('renew') if hasattr(request.data, 'get')
                             else None)
        if renew is None:
            return Response({'error': _('Say whether the coaching should renew.'),
                             'code': 'renew_required', 'field': 'renew'},
                            status=http.HTTP_400_BAD_REQUEST)
        if renew:
            # Agreeing to the monthly charge again: the build must be one that may pay
            # for coaching. Stopping it is allowed everywhere.
            require_purchase_allowed(request, MoneyType.COACHING_1TO1)
        relation, code = set_renewal(client, renew=renew)
        if code == 'no_coaching':
            return Response({'error': _('You do not have a trainer.'),
                             'code': 'no_coaching'}, status=http.HTTP_404_NOT_FOUND)
        if code == 'nothing_to_renew':
            return Response({'error': _('This coaching does not renew, so there is '
                                        'nothing to stop.'),
                             'code': 'nothing_to_renew'}, status=http.HTTP_409_CONFLICT)
        return Response({'coaching': coaching_body(request, relation)})
