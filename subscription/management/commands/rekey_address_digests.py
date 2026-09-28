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
        for row in PayerAddress.objects.order_by("id").iterator():
            current = payer_address.address_digest(row.address)
            if row.address and row.address_digest != current:
                payer_rewritten += 1
                if not dry_run:
                    PayerAddress.objects.filter(pk=row.pk).update(address_digest=current)

        method_rewritten, clashes = 0, []
        methods = PayoutMethod.objects.filter(kind=MethodKind.SHAMCASH).order_by("id")
        for method in methods.iterator():
            current = payouts.address_digest(method.shamcash_address)
            if not method.shamcash_address or method.address_digest == current:
                continue
            clash = (PayoutMethod.objects.filter(trainer_id=method.trainer_id,
                                                 kind=MethodKind.SHAMCASH,
                                                 address_digest=current)
                     .exclude(pk=method.pk).values_list("pk", flat=True).first())
            if clash is not None:
                clashes.append((method.pk, clash))
                continue
            method_rewritten += 1
            if not dry_run:
                with transaction.atomic():
                    PayoutMethod.objects.filter(pk=method.pk).update(address_digest=current)

        verb = "would rewrite" if dry_run else "rewrote"
        self.stdout.write(f"Payer addresses: {verb} {payer_rewritten}. "
                          f"Payout methods: {verb} {method_rewritten}.")
        for method_id, other_id in clashes:
            self.stdout.write(self.style.WARNING(
                f"Payout method {method_id} is the same address as method {other_id} of "
                f"the same trainer: not rewritten; delete one of the two."))
