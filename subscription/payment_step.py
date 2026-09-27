"""The payment step of a checkout, as an adapter (the payments spec, section 6).

"Build the hub so the payment step is an adapter: ManualTransferAdapter today,
ShamCashHostedAdapter later, same session model." A checkout — a plan's or a routine
product's, from the app's buttons or the web hub — is a `Payment` row that
`PaymentService` opens and moves (the session model). What it asks of the money rail is
two things, and they are this module:

* **open** — give the checkout its reference and whatever the gateway says about paying
  it (`opener`: the `initiate(payment)` callable `PaymentService` runs under its lock,
  returning `(payment_method, reference, gateway_response)`);
* **target** — what the customer acts on: where to send the money, how much, and what to
  write in the note (`target`, the session's `payment_target`).

How the money is then FOUND is not here and does not change with the adapter: the
pollers (`subscription.tasks.poll_open_payments`, `poll_expired_checkouts`,
`sweep_unmatched_transfers`) and the webhook confirm a checkout through `PaymentService`
by reference + amount (+ currency), and hold near misses for review.

`ManualTransferAdapter` is the rail today: the customer transfers to the merchant
account with the reference in the note. `ShamCashHostedAdapter` is the named seam for
the hosted ShamCash page once the merchant is approved. It is NOT built: choosing it
raises, and `payment_step` never returns it until it is.
"""
from __future__ import annotations

from .services.payment_gateways import PaymentGatewayError, PaymentGatewayManager


class PaymentStepUnavailable(PaymentGatewayError):
    """The rail cannot take payments right now (the gateway is not configured to). A
    view answers 503 `gateway_unavailable`."""


class PaymentStep:
    """The interface. `gateway_name` is the gateway whose account takes the money."""

    #: The adapter's stable name, as the session's `payment_target.adapter` says it.
    name = ""

    def __init__(self, gateway_name: str = "shamcash"):
        self.gateway_name = gateway_name

    def opener(self, request, *, metadata: dict, service=None):
        """The `initiate(payment)` callable that opens a checkout. `metadata` is what the
        gateway is told about the thing sold (a subscription's or a product's id);
        `service` is a callable returning the gateway service, for a caller that has
        resolved it already."""
        raise NotImplementedError

    def target(self, payment) -> dict:
        """What the customer acts on to pay `payment`."""
        raise NotImplementedError


class ManualTransferAdapter(PaymentStep):
    """Pay by transfer: the reference goes in the note, the pollers match reference +
    amount on the merchant account. The gateway's reconcile mode
    (`subscription/gateways/shamcash.py`)."""

    name = "manual_transfer"

    def service(self):
        """The gateway, through the manager that checks it can take payments at all —
        never constructed directly (an unconfigured account told customers to transfer
        to account "")."""
        try:
            return PaymentGatewayManager.get_service_for_initiation(self.gateway_name)
        except PaymentGatewayError as exc:
            raise PaymentStepUnavailable(str(exc)) from exc

    def opener(self, request, *, metadata: dict, service=None):
        resolve = service or self.service
        gateway_name = self.gateway_name

        def initiate(payment):
            gateway = resolve()
            reference = gateway._generate_reference()
            kind = (payment.metadata or {}).get('kind')
            init = gateway.initiate_payment(
                amount=payment.amount,
                currency=payment.currency,
                user_data={
                    'email': request.user.email,
                    'phone': request.user.phone_number,
                    'name': f"{request.user.first_name} {request.user.last_name}".strip(),
                },
                reference=reference,
                metadata={**metadata, **({'kind': kind} if kind else {}),
                          'description': payment.description},
                callback_url=request.build_absolute_uri(
                    f"/api/subscription/webhook/{gateway_name}/"),
            )
            return gateway_name, reference, init.get('response', {})

        return initiate

    def target(self, payment) -> dict:
        """`{adapter, gateway, account_id, amount, currency, note, payment_url}` — the
        merchant account the gateway named when the checkout opened, the exact amount
        and currency of the checkout (the row's, which is what the pollers match — the
        gateway's echo of it is a float), and the note to write: the reference.
        `payment_url` is null: a transfer has no page."""
        response = payment.gateway_response or {}
        instructions = response.get('instructions') or {}
        return {
            'adapter': self.name,
            'gateway': payment.payment_method,
            'account_id': instructions.get('account_id'),
            'amount': str(payment.amount),
            'currency': payment.currency,
            'note': payment.gateway_transaction_reference,
            'payment_url': response.get('payment_url'),
        }


class ShamCashHostedAdapter(PaymentStep):
    """The hosted ShamCash page, once ShamCash approves the merchant (the spec, section
    6: "the target state"). NOT BUILT: its initiation endpoint, its fields and its
    webhook are known only after approval. When it is, it opens the hosted session
    here, answers its `payment_url` as the target, and the pollers and the webhook
    confirm it exactly as they confirm a transfer — the session model does not move."""

    name = "shamcash_hosted"

    def opener(self, request, *, metadata: dict, service=None):
        raise NotImplementedError("The hosted ShamCash checkout is not built: it waits for "
                                  "the merchant approval (payments spec, section 6)")

    def target(self, payment) -> dict:
        raise NotImplementedError("The hosted ShamCash checkout is not built")


#: Every adapter, by name.
ADAPTERS = {ManualTransferAdapter.name: ManualTransferAdapter,
            ShamCashHostedAdapter.name: ShamCashHostedAdapter}


def payment_step(gateway_name: str = "shamcash") -> PaymentStep:
    """The adapter every checkout opens through today: the manual transfer. The one
    place the hosted page will be chosen when it exists."""
    return ManualTransferAdapter(gateway_name)
