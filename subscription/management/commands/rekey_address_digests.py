"""`manage.py rekey_address_digests` — rewrite the keyed digests of saved ShamCash
addresses under the CURRENT `SECRET_KEY`, after a key rotation (SIDE_BUGS S-232).

Two tables compare addresses by a keyed digest, never by the encrypted address itself:
a customer's saved payer address (`subscription.PayerAddress`, the second matching
signal of the payments spec, section 6) and a trainer's ShamCash payout methods
(`payouts.PayoutMethod`, whose per-trainer unique constraint compares digests). Both
MATCH under the current key and every `SECRET_KEY_FALLBACKS` entry
(`address_digests`), so a rotation is hitless while the old key stays in
`DJANGO_OLD_SECRET_KEY_1`. Run this once the new key is live and BEFORE the old key is
dropped: every row is then written under the new key and the old one can go.

Idempotent, and safe to run at any time: a row already under the current key is left
alone. A payout method whose new digest another method of the same trainer already
carries (the same address saved twice across a rotation, before the fix) is not
rewritten — the constraint would refuse it — and is reported for an operator to delete
one of the two. `--dry-run` counts without writing.

Each row is re-read under its own lock and its digest computed from the address it holds
THEN (SIDE_BUGS S-261): `payer_address.save` takes the same lock to replace an address,
and a write by primary key alone, computed from an earlier read, could give a newly
saved address its predecessor's digest — after which that customer's transfers stopped
matching their saved address.
"""
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = ("Rewrite saved ShamCash payer and payout address digests under the current "
            "SECRET_KEY (after a key rotation, before the old key is dropped).")

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Count what would be rewritten; write nothing.")

    def handle(self, *args, dry_run=False, **options):
        from payouts import services as payouts
        from payouts.models import MethodKind, PayoutMethod
        from subscription import payer_address
        from subscription.models import PayerAddress

        payer_rewritten = 0
        for pk in PayerAddress.objects.order_by("id").values_list("pk", flat=True):
            with transaction.atomic():
                row = PayerAddress.objects.select_for_update().filter(pk=pk).first()
                if row is None:
                    continue
                current = payer_address.address_digest(row.address)
                if row.address and row.address_digest != current:
                    payer_rewritten += 1
                    if not dry_run:
                        PayerAddress.objects.filter(pk=pk).update(address_digest=current)

        method_rewritten, clashes = 0, []
        methods = PayoutMethod.objects.filter(kind=MethodKind.SHAMCASH).order_by("id")
        for pk in methods.values_list("pk", flat=True):
            with transaction.atomic():
                method = PayoutMethod.objects.select_for_update().filter(pk=pk).first()
                if method is None:
                    continue
                current = payouts.address_digest(method.shamcash_address)
                if not method.shamcash_address or method.address_digest == current:
                    continue
                clash = (PayoutMethod.objects.filter(trainer_id=method.trainer_id,
                                                     kind=MethodKind.SHAMCASH,
                                                     address_digest=current)
                         .exclude(pk=pk).values_list("pk", flat=True).first())
                if clash is not None:
                    clashes.append((pk, clash))
                    continue
                method_rewritten += 1
                if not dry_run:
                    PayoutMethod.objects.filter(pk=pk).update(address_digest=current)

        verb = "would rewrite" if dry_run else "rewrote"
        self.stdout.write(f"Payer addresses: {verb} {payer_rewritten}. "
                          f"Payout methods: {verb} {method_rewritten}.")
        for method_id, other_id in clashes:
            self.stdout.write(self.style.WARNING(
                f"Payout method {method_id} is the same address as method {other_id} of "
                f"the same trainer: not rewritten; delete one of the two."))
