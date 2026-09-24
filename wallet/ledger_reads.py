"""Filtering a person's own ledger, and summing it by month (BACKEND_BUGS B-101).

TrainerEarnings draws a six-month chart («أرباحك الشهرية») and «من أين أرباح أيلول»: this
month's income split into coaching, the trainer's share of plans and program sales. The
ledger answered 25 rows a page with no filter, so the app read one page of 100 rows and
summed it itself: a trainer with more than 100 rows in six months got a truncated chart.

Two reads, one set of filters:

* `GET /api/wallet/transactions/` takes `?since=&until=&money_type=&kind=` (all optional;
  with none it is the call it always was).
* `GET /api/wallet/transactions/summary/` answers the sums per calendar month and per
  kind of money for a window of at most `SUMMARY_MAX_MONTHS`, in two aggregate queries
  whatever the number of rows.

THE RULES THE APP CAN RELY ON

* `since` / `until` are dates, `YYYY-MM-DD`, INCLUSIVE, in the server's zone
  (`settings.TIME_ZONE`, the zone every ledger day and month is cut in). `until` covers
  the whole of its day. A half-open range on `created_at`, never a function of the
  column, so the ledger's `(wallet, -created_at, -id)` indexes serve it.
* `money_type` is one or several of the four (`policy.money.MoneyType`); `kind` one or
  several of `wallet.activity.KINDS`. Repeat the parameter or separate with commas.
  Several values mean ANY of them; `money_type` and `kind` together mean both.
* Every amount is in the wallet's own currency: a wallet holds one, and every row that
  touches it is in it (the other leg of an exchange is not this wallet's row).
* A refusal is 400 with its own code: `bad_date`, `date_range_reversed`,
  `date_range_too_long` (summary only), `bad_money_type`, `bad_kind`.

Nothing here is a price or an offer: it is money that already moved, so it is the same on
every `X-App-Channel`, like the ledger itself.
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from decimal import Decimal

from django.utils import timezone
from django.utils.translation import gettext as _

from policy.money import MoneyType

#: The dates a filter may name. Outside them a date is refused as `bad_date`: the ledger
#: starts in 2026, and a year-1 or year-9999 date overflows the zone conversion.
EARLIEST = datetime.date(2000, 1, 1)
LATEST = datetime.date(2999, 12, 31)

#: The widest window the summary sums, in calendar months (the task: up to 12).
SUMMARY_MAX_MONTHS = 12
#: The window when the app names none: the design's chart, this month and the five before.
SUMMARY_DEFAULT_MONTHS = 6

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CENT = Decimal("0.01")


class LedgerFilterRefused(Exception):
    """A filter the server will not guess at. `code` is the contract; `message` is for
    the person; `extra` goes into the body beside them."""

    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


@dataclass(frozen=True)
class LedgerFilters:
    since: datetime.date | None = None
    until: datetime.date | None = None
    money_types: tuple = ()
    kinds: tuple = ()

    def apply(self, queryset, wallet):
        """`queryset` (rows of `wallet`'s ledger) narrowed to these filters."""
        from django.db.models import Q

        from .activity import kind_q

        if self.since is not None:
            queryset = queryset.filter(created_at__gte=start_of(self.since))
        if self.until is not None:
            queryset = queryset.filter(
                created_at__lt=start_of(self.until + datetime.timedelta(days=1)))
        if self.money_types:
            queryset = queryset.filter(money_type__in=self.money_types)
        if self.kinds:
            either = Q()
            for kind in self.kinds:
                either |= kind_q(kind, wallet)
            queryset = queryset.filter(either)
        return queryset


def start_of(day: datetime.date) -> datetime.datetime:
    """Midnight at the start of `day` in the server's zone, as an aware datetime."""
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time.min),
                               timezone.get_default_timezone())


def _values(params, name) -> list[str]:
    """Every value of `name`, repeated or comma-separated, stripped, in order, once."""
    getlist = getattr(params, "getlist", None)
    raw = getlist(name) if getlist is not None else [params.get(name)]
    out = []
    for value in raw:
        for part in str(value or "").split(","):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


def _date(params, name):
    raw = params.get(name)
    if raw is None or str(raw).strip() == "":
        return None
    text = str(raw).strip()
    value = None
    if _ISO_DATE.match(text):
        try:
            value = datetime.date.fromisoformat(text)
        except ValueError:
            value = None
    if value is None or not (EARLIEST <= value <= LATEST):
        raise LedgerFilterRefused(
            "bad_date",
            _("%(field)s must be a date written YYYY-MM-DD, between 2000 and 2999.")
            % {"field": name},
            field=name)
    return value


def parse_filters(params) -> LedgerFilters:
    """The filters `params` asks for, or `LedgerFilterRefused`."""
    from .activity import KINDS

    since, until = _date(params, "since"), _date(params, "until")
    if since is not None and until is not None and since > until:
        raise LedgerFilterRefused("date_range_reversed",
                                  _("since must not be after until."))
    money_types = []
    for value in _values(params, "money_type"):
        value = value.upper()
        if value not in MoneyType.values:
            raise LedgerFilterRefused("bad_money_type", _("That kind of money is unknown."),
                                      money_type_options=list(MoneyType.values))
        if value not in money_types:
            money_types.append(value)
    kinds = _values(params, "kind")
    for value in kinds:
        if value not in KINDS:
            raise LedgerFilterRefused("bad_kind", _("That kind of activity is unknown."),
                                      kind_options=list(KINDS))
    return LedgerFilters(since=since, until=until, money_types=tuple(money_types),
                         kinds=tuple(kinds))


def _month_index(day: datetime.date) -> int:
    return day.year * 12 + day.month - 1


def _month_start(index: int) -> datetime.date:
    return datetime.date(index // 12, index % 12 + 1, 1)


def summary_window(filters: LedgerFilters, today=None) -> LedgerFilters:
    """`filters` with the summary's window filled in and checked.

    `until` defaults to today; `since` to the first day of the month
    `SUMMARY_DEFAULT_MONTHS - 1` before `until`'s. A window touching more than
    `SUMMARY_MAX_MONTHS` calendar months is `date_range_too_long`.
    """
    today = today or timezone.localdate()
    until = filters.until or max(today, filters.since or today)
    since = filters.since or _month_start(_month_index(until) - (SUMMARY_DEFAULT_MONTHS - 1))
    if since > until:
        raise LedgerFilterRefused("date_range_reversed",
                                  _("since must not be after until."))
    if _month_index(until) - _month_index(since) + 1 > SUMMARY_MAX_MONTHS:
        raise LedgerFilterRefused(
            "date_range_too_long",
            _("The summary covers at most %(months)d months.") % {"months": SUMMARY_MAX_MONTHS},
            max_months=SUMMARY_MAX_MONTHS)
    return LedgerFilters(since=since, until=until, money_types=filters.money_types,
                         kinds=filters.kinds)


def _counterparty(wallet):
    """`wallet.activity.counterparty_id`, as an expression (text, or NULL for nobody), so
    `people` can be counted in the database. Same order: the trainer named on the row,
    then the client, then the agent, then whoever owns the other wallet unless it is the
    platform's own (escrow, revenue)."""
    from django.db.models import Case, CharField, F, Q, Value, When
    from django.db.models.fields.json import KeyTextTransform
    from django.db.models.functions import Cast

    from .activity import ledger_owner_ids

    me = str(wallet.owner_id)
    falsy = ["", "0", "false"]
    platform = [str(i) for i in ledger_owner_ids()]
    annotations = {
        "ledger_trainer": KeyTextTransform("trainer_id", "metadata"),
        "ledger_client": KeyTextTransform("client_id", "metadata"),
        "ledger_agent": KeyTextTransform("agent_id", "metadata"),
        "ledger_other": Case(
            When(destination_wallet=wallet,
                 then=Cast("source_wallet__owner_id", CharField())),
            default=Cast("destination_wallet__owner_id", CharField()),
            output_field=CharField()),
    }
    other = Q(ledger_other__isnull=False)
    if platform:
        other &= ~Q(ledger_other__in=platform)
    who = Case(
        When(Q(ledger_trainer__isnull=False) & ~Q(ledger_trainer__in=falsy + [me]),
             then=F("ledger_trainer")),
        When(Q(ledger_client__isnull=False) & ~Q(ledger_client__in=falsy + [me]),
             then=F("ledger_client")),
        When(Q(ledger_agent__isnull=False) & ~Q(ledger_agent__in=falsy),
             then=F("ledger_agent")),
        When(other, then=F("ledger_other")),
        default=Value(None), output_field=CharField())
    return annotations, who


def _money(value) -> str:
    return str((value or Decimal("0")).quantize(CENT))


def _block(amount_in, amount_out, count, people=None) -> dict:
    block = {"in": _money(amount_in), "out": _money(amount_out),
             "net": _money((amount_in or 0) - (amount_out or 0)), "count": count}
    if people is not None:
        block["people"] = people
    return block


def monthly_summary(queryset, wallet, filters: LedgerFilters) -> dict:
    """The body of `GET /api/wallet/transactions/summary/` for `queryset` (the wallet's
    whole ledger) under `filters` (already through `summary_window`).

    `months`: every calendar month the window touches, oldest first, each `{month
    ("2026-09"), in, out, net, count, by_money_type}`; `by_money_type` has all four keys,
    each `{in, out, net, count, people}`. `totals`: the same over the window, whose
    `people` are counted across it (a client paid in two months is one person).
    `in` is money that reached this wallet, `out` money that left it; `people` is how
    many different people the rows were with (`counterparty` on the ledger), so a
    trainer's coaching `people` is the design's «16 عميلاً» and `count` of
    `ROUTINE_PRODUCT` in is programs sold. The first and last month hold only the days
    inside the window.
    """
    from django.db.models import Count, Q, Sum
    from django.db.models.functions import TruncMonth

    zone = timezone.get_default_timezone()
    rows = filters.apply(queryset, wallet).order_by()
    annotations, who = _counterparty(wallet)
    rows = rows.annotate(**annotations).annotate(ledger_who=who)
    incoming = Q(destination_wallet=wallet)

    grouped = (rows.annotate(ledger_month=TruncMonth("created_at", tzinfo=zone))
               .values("ledger_month", "money_type")
               .annotate(amount_in=Sum("amount", filter=incoming),
                         amount_out=Sum("amount", filter=~incoming),
                         rows=Count("id"),
                         people=Count("ledger_who", distinct=True))
               .order_by("ledger_month", "money_type"))
    window_people = dict(rows.values("money_type")
                         .annotate(people=Count("ledger_who", distinct=True))
                         .order_by("money_type")
                         .values_list("money_type", "people"))

    types = list(MoneyType.values)
    first, last = _month_index(filters.since), _month_index(filters.until)
    cells = {}
    for row in grouped:
        month = row["ledger_month"]
        if isinstance(month, datetime.datetime):
            if timezone.is_aware(month):
                month = timezone.localtime(month, zone)
            month = month.date()
        cells[(_month_index(month), row["money_type"])] = row

    months = []
    totals = {t: [Decimal("0"), Decimal("0"), 0] for t in types}
    for index in range(first, last + 1):
        by_type = {}
        month_in, month_out, month_count = Decimal("0"), Decimal("0"), 0
        for money_type in types:
            cell = cells.get((index, money_type)) or {}
            amount_in = cell.get("amount_in") or Decimal("0")
            amount_out = cell.get("amount_out") or Decimal("0")
            count = cell.get("rows") or 0
            by_type[money_type] = _block(amount_in, amount_out, count,
                                         cell.get("people") or 0)
            month_in += amount_in
            month_out += amount_out
            month_count += count
            totals[money_type][0] += amount_in
            totals[money_type][1] += amount_out
            totals[money_type][2] += count
        start = _month_start(index)
        months.append({"month": f"{start.year:04d}-{start.month:02d}",
                       **_block(month_in, month_out, month_count),
                       "by_money_type": by_type})

    total_in = sum((v[0] for v in totals.values()), Decimal("0"))
    total_out = sum((v[1] for v in totals.values()), Decimal("0"))
    total_count = sum(v[2] for v in totals.values())
    return {
        "currency": wallet.currency,
        "since": filters.since.isoformat(),
        "until": filters.until.isoformat(),
        "money_type": list(filters.money_types),
        "kind": list(filters.kinds),
        "months": months,
        "totals": {**_block(total_in, total_out, total_count),
                   "by_money_type": {t: _block(totals[t][0], totals[t][1], totals[t][2],
                                               window_people.get(t, 0))
                                     for t in types}},
    }
