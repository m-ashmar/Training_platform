"""The ShamCash address a customer pays from, and the second matching signal it gives
(the spec, section 6: "Save the address, not the account ... Add the sender's saved
address as a second signal so a missing or mistyped note can still be matched for
operator review").

Saving. One address per account (`PayerAddress`), typed twice, in the same form and by
the same rule as a trainer's payout address (`payouts.services.normalise_address` /
`address_is_valid`): spaces and dashes removed, lower case, 8 to 64 ASCII letters and
digits — which a 4-to-6-digit PIN or OTP can never be. Never a password, PIN, OTP or
session: nothing asks for one.

Matching. A transfer's sender, as the gateway reports it, is normalised the same way
and compared by its keyed digest. The match is EVIDENCE for the operator's review queue
and nothing more: it can point the queue at one customer's checkout (`likely_payment`),
and it is written onto the queue row (`TransferReview.sender_match_payment_ids`) and the
held payment's event. It never completes a payment — only the reference with the exact
amount does, or an operator. An address saved by more than one account points at nobody.

CONFIRM with ShamCash which field of a transaction carries the sender's ACCOUNT ADDRESS
(the adapter reads `sender_address`, `sender_account`, `from_account`, `from` and
`sender`). Only a whole field equal to a saved address matches; nothing is guessed out
of a longer string.
"""
from __future__ import annotations

from django.db import transaction
from django.utils.crypto import salted_hmac
from django.utils.translation import gettext as _

from payouts.services import address_is_valid, digest_keys, grouped, normalise_address

#: The keys of a raw ShamCash transaction that may hold the sender's account address.
SENDER_KEYS = ('sender_address', 'sender_account', 'from_account', 'from', 'sender')

_DIGEST_SALT = 'subscription.payer_address'


def address_digest(normalised: str) -> str:
    """The keyed hash a sender is compared by. Its own key, not the payout table's, so
    the two tables of addresses cannot be joined on their digests. This is the digest a
    row is WRITTEN with: under the current `SECRET_KEY`."""
    return salted_hmac(_DIGEST_SALT, normalised).hexdigest()


def address_digests(normalised: str) -> list[str]:
    """Every digest a saved row of this address can carry, for MATCHING: under the
    current key and under each `SECRET_KEY_FALLBACKS` entry (SIDE_BUGS S-232). Without
    the fallbacks a key rotation orphaned every saved address: the second signal went
    quiet for every customer, and a stranger who saved a victim's address after the
    rotation became its only owner. `manage.py rekey_address_digests` rewrites old rows
    under the current key before the old key is dropped."""
    digests = [salted_hmac(_DIGEST_SALT, normalised, secret=key).hexdigest()
               for key in digest_keys()]
    return list(dict.fromkeys(digests))


class Refused(Exception):
    """A refusal the app branches on: `code`, an HTTP `status`, a translated message."""

    def __init__(self, code: str, message, status: int = 400):
        super().__init__(code)
        self.code = code
        self.message = message
        self.status = status


def payload(row) -> dict:
    """What the profile screen reads: the address grouped in fours as the payout screen
    shows one ("0a3f 91c2 7be4 55d0"), or null when none is saved."""
    if row is None:
        return {'address': None, 'saved_at': None, 'updated_at': None}
    return {'address': grouped(row.address), 'saved_at': row.created_at,
            'updated_at': row.updated_at}


def save(user, address, address_confirm):
    """Save or replace the caller's payer address. Raises `Refused`."""
    from .models import PayerAddress

    address, confirm = normalise_address(address), normalise_address(address_confirm)
    if not address:
        raise Refused('payer_address_required', _('Type your ShamCash account address.'))
    if not address_is_valid(address):
        raise Refused('payer_address_invalid',
                      _('That does not look like a ShamCash account address.'))
    if address != confirm:
        raise Refused('payer_address_mismatch', _('The two addresses you typed do not match.'))
    with transaction.atomic():
        row, _created = PayerAddress.objects.select_for_update().get_or_create(
            user=user, defaults={'address': address, 'address_digest': address_digest(address)})
        if not _created:
            row.address = address
            row.address_digest = address_digest(address)
            row.save(update_fields=['address', 'address_digest', 'updated_at'])
    return row


def remove(user) -> bool:
    """Forget the caller's payer address. Whether there was one."""
    from .models import PayerAddress

    deleted, _rows = PayerAddress.objects.filter(user=user).delete()
    return bool(deleted)


# ------------------------------------------------------------------ the signal
def sender_digests(facts) -> set[str]:
    """The digests of every field of a transfer that could be its sender's account
    address: each whole field, normalised, that has an address's form."""
    raw = facts.get('gateway_data') if isinstance(facts, dict) else None
    values = []
    if isinstance(raw, dict):
        values += [raw.get(key) for key in SENDER_KEYS]
    if isinstance(facts, dict):
        values.append(facts.get('sender'))
    digests = set()
    for value in values:
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            continue
        normalised = normalise_address(value)
        if normalised and address_is_valid(normalised):
            digests.update(address_digests(normalised))
    return digests


def owners_of_sender(facts) -> set:
    """The ids of the accounts that saved this transfer's sender address."""
    from .models import PayerAddress

    digests = sender_digests(facts)
    if not digests:
        return set()
    return set(PayerAddress.objects.filter(address_digest__in=digests)
               .values_list('user_id', flat=True))


def sender_matches(facts, payment_ids) -> list[str]:
    """Which of `payment_ids` belong to a customer who saved this transfer's sender
    address, as strings, in the order given."""
    from .models import Payment

    ids = [pid for pid in payment_ids if pid]
    owners = owners_of_sender(facts) if ids else set()
    if not owners:
        return []
    mine = {str(pk) for pk in Payment.objects.filter(pk__in=ids,
                                                     subscription__user_id__in=owners)
            .values_list('pk', flat=True)}
    return [str(pid) for pid in ids if str(pid) in mine]


def likely_payment(facts, open_qs, closed_qs=None):
    """The checkout this transfer most likely paid, by its sender's saved address alone,
    or None.

    Only when exactly ONE account saved the sender's address: that account's checkout in
    `open_qs` (a subscription has one open checkout at a time), else its newest in
    `closed_qs` (checkouts closed within the late-money window, which `hold_for_review`
    can still hold). Both are already narrowed by the caller to the transfer's exact
    amount and currency and to checkouts opened before it."""
    owners = owners_of_sender(facts)
    if len(owners) != 1:
        return None
    mine = list(open_qs.filter(subscription__user_id__in=owners)
                .order_by('created_at', 'id').values_list('pk', flat=True)[:2])
    if mine:
        return mine[0] if len(mine) == 1 else None
    if closed_qs is None:
        return None
    return (closed_qs.filter(subscription__user_id__in=owners)
            .order_by('-created_at', '-id').values_list('pk', flat=True).first())
