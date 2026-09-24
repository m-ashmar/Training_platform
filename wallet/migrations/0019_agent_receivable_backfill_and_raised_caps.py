"""Two data steps for plan step 9, both idempotent.

1. **The receivable of every top-up already made.** Every agent top-up is cash the agent
   holds for the platform (the spec, section 8), and nothing was ever settled, so each
   one written before `AgentReceivableEntry` existed is posted now, dated with its own
   transaction's time, with the reversals of any of them — in the order they happened,
   so each row's `balance_after` is the running total it would have had. A top-up is an
   AGENT top-up when its metadata names `agent_id` (`wallet.activity.kind_of`), never
   the mint leg of a plan payment, which is also a `topup` row.

2. **The old default caps are lifted.** An agent still on the pair every agent used to be
   created with — 200 SYP a day, 5,000 a month — gets the new defaults
   (`settings.AGENT_DEFAULT_*`: the per-top-up cap, the daily and monthly caps and the
   receivable ceiling, all in pounds). "200 SYP a day cannot cover one Basic plan at 1,000
   SYP" (the spec). A pair an operator chose is left exactly as it was; its per-top-up cap
   and ceiling stay empty (none of its own; the ceiling reads as its monthly cap).
"""
from decimal import Decimal

from django.db import migrations

OLD_DAILY, OLD_MONTHLY = Decimal("200"), Decimal("5000")


def backfill_the_receivable(apps, schema_editor):
    Transaction = apps.get_model("wallet", "Transaction")
    AgentProfile = apps.get_model("wallet", "AgentProfile")
    Entry = apps.get_model("wallet", "AgentReceivableEntry")

    profiles = {p.user_id: p for p in AgentProfile.objects.all()}
    posted = set(Entry.objects.exclude(transaction__isnull=True)
                 .values_list("transaction_id", flat=True))
    topups = [tx for tx in Transaction.objects.filter(
        tx_type="topup", metadata__has_key="agent_id").order_by("created_at", "id")
        if tx.actor_id in profiles]
    by_id = {tx.pk: tx for tx in topups}
    reversals = list(Transaction.objects.filter(
        tx_type="reversal", reverses_id__in=list(by_id)).order_by("created_at", "id"))

    events = [(tx.created_at, tx.pk, "topup", tx, profiles[tx.actor_id], tx.amount)
              for tx in topups]
    events += [(tx.created_at, tx.pk, "reversal", tx,
                profiles[by_id[tx.reverses_id].actor_id], -tx.amount)
               for tx in reversals]
    events.sort(key=lambda e: (e[0], e[1]))

    running = {}
    for (profile_id, currency), total in _existing_totals(Entry).items():
        running[(profile_id, currency)] = total
    for created_at, _pk, kind, tx, profile, amount in events:
        if tx.pk in posted:
            continue
        key = (profile.pk, tx.currency)
        running[key] = running.get(key, Decimal("0")) + amount
        Entry.objects.create(agent=profile, kind=kind, currency=tx.currency,
                             amount=amount, balance_after=running[key],
                             money_type="WALLET_MOVE", transaction=tx,
                             created_at=created_at)


def _existing_totals(Entry):
    from django.db.models import Sum

    return {(row["agent_id"], row["currency"]): row["total"]
            for row in Entry.objects.values("agent_id", "currency")
            .annotate(total=Sum("amount")).order_by()}


def raise_the_old_default_caps(apps, schema_editor):
    from django.conf import settings

    AgentProfile = apps.get_model("wallet", "AgentProfile")
    AgentProfile.objects.filter(daily_limit=OLD_DAILY, monthly_limit=OLD_MONTHLY).update(
        daily_limit=settings.AGENT_DEFAULT_DAILY_LIMIT,
        monthly_limit=settings.AGENT_DEFAULT_MONTHLY_LIMIT,
        per_transaction_limit=settings.AGENT_DEFAULT_PER_TRANSACTION_LIMIT,
        receivable_ceiling=settings.AGENT_DEFAULT_RECEIVABLE_CEILING)


def forwards(apps, schema_editor):
    backfill_the_receivable(apps, schema_editor)
    raise_the_old_default_caps(apps, schema_editor)


class Migration(migrations.Migration):

    dependencies = [
        ("wallet", "0018_agent_shell_limits_receivable_settlements_pairing"),
    ]

    operations = [
        # Nothing to undo: the entries are dropped with the table by 0018's reverse, and
        # a cap is not put back to a number that could not buy a plan.
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
