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
                      | "payment_due"              a renewal was tried and the wallet
                                                   could not cover it
                      | "unpaid",                  support returned month 1; nothing
                                                   renews until support resolves it
            "period": {"number", "starts_at", "ends_at"} | null,   the latest paid month
            "renews_at": datetime | null,          when the next month is charged
            "grace_until": datetime | null,        payment_due only: after this the
                                                   coaching ends
            "ending": bool,                        the trainer stops when this month ends
                                                   (`period.ends_at`); nothing renews
            "coaching_price", "money_type"}}       ONLY on a build that may show a
                                                   coaching fee (not `ios` today)

    Each month is charged from the wallet automatically when the last one ends; the
    platform's commission is taken from every month and is not shown to the client.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from policy.money import MoneyType
        from policy.services import can_show_price

        from .models import TrainerClientRelation
        from .trainer_relations import price_of

        client = request.user
        if not client.is_client:
            raise PermissionDenied(_('Only clients have coaching.'), code='clients_only')
        relation = (TrainerClientRelation.objects.select_related('trainer')
                    .filter(client=client, status='approved').first())
        if relation is None:
            return Response({'coaching': None})
        body = coaching_status(relation)
        if can_show_price(request, MoneyType.COACHING_1TO1):
            # What the next month costs: the latest month's price, which is what renews.
            latest = relation.periods.order_by('-number').first()
            body['coaching_price'] = str(latest.price if latest else price_of(relation))
            body['money_type'] = MoneyType.COACHING_1TO1.value
        return Response({'coaching': body})
