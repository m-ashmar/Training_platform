"""The ShamCash address a customer pays from, on their profile (the spec, section 6:
"Save the address, not the account"). See `subscription/payer_address.py`."""
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from training_platform.query_params import body_dict

from . import payer_address


class PayerAddressView(APIView):
    """`GET`, `PUT`, `DELETE /api/subscription/v1/payer-address/` — the public ShamCash
    account address the caller pays FROM, saved on their profile.

    What it is for: when a transfer reaches our merchant account with the right amount
    and a missing or mistyped reference, a sender address that matches the one saved
    here points the operator's review queue at the caller's checkout (the checkout shows
    `needs_review` with `review.reason` `reference_missing`, as it already can) — once
    the address is proven: once a transfer FROM it has confirmed one of the caller's
    checkouts by its reference (SIDE_BUGS S-241; changing the address unproves it).
    Until then a match is only noted for the operator. It is
    evidence for a person and nothing more: it NEVER confirms a payment on its own —
    only the reference with the exact amount does, or an operator — so the reference
    still belongs in the transfer's note. An address, never an account: no password,
    PIN, OTP or session is asked for, here or anywhere.

    **GET** — 200 `{address, saved_at, updated_at}`: `address` grouped in fours as the
    payout screen shows one ("0a3f 91c2 7be4 55d0"), or all three null when none is saved.

    **PUT** — save it, or replace the one saved. Body `{"address": "...",
    "address_confirm": "..."}`: typed twice. Spaces and dashes are ignored and case does
    not matter; what is kept is 8 to 64 ASCII letters and digits. 200 with the GET body.

    **DELETE** — forget it. 204, also when none was saved.

    | Status | `code` | When |
    |---|---|---|
    | 400 | `payer_address_required` | PUT with no `address` |
    | 400 | `payer_address_invalid` | not 8-64 letters and digits once spaces and dashes are removed |
    | 400 | `payer_address_mismatch` | `address` and `address_confirm` differ |
    | 400 | `validation_error` | the body is not a JSON object |
    | 401 | `not_authenticated` | no token |

    The caller's own record, like a trainer's payout methods: identical on every build
    (no price, nothing to buy, no instruction to pay). Encrypted at rest; in the privacy
    export and erased with the account (`payer_address`).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .models import PayerAddress

        row = PayerAddress.objects.filter(user=request.user).first()
        return Response(payer_address.payload(row))

    def put(self, request):
        body = body_dict(request)
        try:
            row = payer_address.save(request.user, body.get('address') or '',
                                     body.get('address_confirm') or '')
        except payer_address.Refused as exc:
            return Response({'error': str(exc.message), 'code': exc.code}, status=exc.status)
        return Response(payer_address.payload(row))

    def delete(self, request):
        payer_address.remove(request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)
