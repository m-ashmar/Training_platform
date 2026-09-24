from decimal import Decimal
from rest_framework import serializers
from .models import Wallet, Transaction, AgentProfile, AgentAPIKey, WalletAuditLog, IdempotencyKey
# The settling window (BACKEND_BUGS B-097), as mixins so its fields stay out of the two
# `Meta.fields` lists below: see wallet/settling.py.
from .settling import TransactionSettlingFields, WalletSettlingFields
# `withdrawal` on a ledger row (B-125); the module imports nothing of `wallet` at load.
from payouts.ledger_fields import TransactionWithdrawalFields

_MIN_AMOUNT = Decimal("0.01")


class WalletSerializer(WalletSettlingFields, serializers.ModelSerializer):
    """The wallet as the Wallet and TopUp screens draw it.

    `held` and `held_for` are the "إضافةً إلى 12,000 محجوزة لمدربك رامي" line: money
    that has left the balance into escrow for a coaching month and is neither spent
    nor available. Escrow is a real wallet with a real balance, so without this the
    client's screen could only show the smaller number and no reason for it.

    `account_number` is what the TopUp screen tells the client to read out to an agent
    (`wallet.utils.account_number_for`) — an address, not a secret.

    THE TRAINER'S SIDE OF ESCROW (BACKEND_BUGS B-096) — TrainerEarnings' «محجوز لدى
    المنصة · 3 عملاء بانتظار أول برنامج», read by `users.trainer_escrow.held_for_trainer`:

    * `held_for_trainer` — what the trainer will RECEIVE when each held month 1 is
      released (the client's fee less the platform's commission at the rate copied onto
      that month), summed, in this wallet's currency. `"0.00"` for anyone who is not a
      trainer, and for a trainer with nothing held.
    * `held_for_trainer_gross` — the same months at the client's fee, before commission.
    * `awaiting_first_program` — how many clients have month 1 held and no first program
      yet (the status `GET /api/v1/coaching/current/` gives the client for the same
      month). `0` for anyone who is not a trainer.
    * `held_for_trainer_items` — one per such client, newest hold first: `relation_id`,
      `client` `{id, name, role}`, `status` (`awaiting_first_program`), `kind`
      (`trainer_fee_hold`, the ledger's kind for this money), `reason`, `month`,
      `requested_at`, `held_at`, `amount` (client's fee) = `commission` + `net`,
      `commission_rate` (a fraction, `"0.2"`), `currency`, `charged_amount` /
      `charged_currency` (the month as the client paid it) and `estimated` (true when
      the month crosses a currency, so `pay_out` will convert the trainer's share at
      the rate of the day it pays). An item whose amount cannot be put in this wallet's
      currency keeps its own `currency` and is left out of the two sums. `[]` for
      anyone who is not a trainer. These are NOT ledger rows (no `reference_id`): the
      hold is one client -> escrow row, and nothing is written for the trainer until
      it is released.

    WHAT THE PLATFORM OWES THE TRAINER (the payouts review, 2026-09-24), read by
    `users.trainer_escrow.owed_to_trainer`. A month paid out across two currencies while
    the operator's rate was missing or stale — month 1 at the first program, or a
    renewal — runs, but the trainer's share stays in escrow, owed, until the hourly run
    finds a fresh rate (`users.coaching.pay_out`). It is not held and not yet a ledger
    row, so it was in none of the figures above. Two keys of its own; nothing above
    changes meaning:

    * `owed_to_trainer` — the owed shares, summed, in this wallet's currency. `"0.00"`
      for anyone who is not a trainer and for a trainer owed nothing.
    * `owed_to_trainer_items` — one per such month, newest first: `period_id`,
      `relation_id` (null once the relation is gone), `client` `{id, name, role}`,
      `status` (`payout_owed`), `kind` (`trainer_fee_payout`, the kind of the ledger row
      that arrives when it is paid), `reason`, `month`, `owed_since`, `starts_at`,
      `amount` = `commission` + `net`, `commission_rate`, `currency`, `charged_amount` /
      `charged_currency` and `estimated`, all as in `held_for_trainer_items` (an item
      whose amount cannot be put in this wallet's currency keeps its own and is left
      out of the sum). `[]` for anyone else. Not ledger rows: when the share is paid the
      item goes and a `trainer_fee_payout` row arrives.

    Available and settling (B-097) are another builder's; these four keys stay in their
    own methods below so the two merge line by line.

    None of it is a price or a way to buy anything, so all of it is answered on every
    `X-App-Channel`; the spec lets even the iOS build show the balance and the history.
    """
    held = serializers.SerializerMethodField()
    held_for = serializers.SerializerMethodField()
    account_number = serializers.SerializerMethodField()
    currency_locked = serializers.SerializerMethodField()
    currency_options = serializers.SerializerMethodField()
    # B-096: the trainer's side of escrow.
    held_for_trainer = serializers.SerializerMethodField()
    held_for_trainer_gross = serializers.SerializerMethodField()
    awaiting_first_program = serializers.SerializerMethodField()
    held_for_trainer_items = serializers.SerializerMethodField()
    # The payouts review, 2026-09-24: a trainer's share owed in escrow.
    owed_to_trainer = serializers.SerializerMethodField()
    owed_to_trainer_items = serializers.SerializerMethodField()

    class Meta:
        model = Wallet
        fields = ["id", "owner", "owner_type", "balance", "currency", "created_at",
                  "updated_at", "held", "held_for", "account_number",
                  "currency_locked", "currency_options",
                  # B-096: the trainer's side of escrow.
                  "held_for_trainer", "held_for_trainer_gross", "awaiting_first_program",
                  "held_for_trainer_items",
                  # The payouts review, 2026-09-24: a trainer's share owed in escrow.
                  "owed_to_trainer", "owed_to_trainer_items"]
        read_only_fields = fields

    def get_currency_locked(self, obj):
        """True once this person's currency is decided by money that has moved.

        `currency` IS the person's currency (the owner, 2026-09-21: they pick one and
        every price and charge is in it). While this is false the app may offer the
        choice; once it is true the choice is gone and `PUT /api/wallet/currency/`
        answers `currency_locked`.
        """
        from .models import wallet_is_untouched

        return not wallet_is_untouched(obj)

    def get_currency_options(self, obj):
        """The currencies this person could pick. The app must not hardcode the list."""
        from .currencies import CURRENCY_CODES

        return list(CURRENCY_CODES)

    def _hold(self, obj):
        """The client's live coaching hold as `(amount, relation)`, or `(0, None)`.

        `TrainerClientRelation.hold_reference` is the identity of the hold — the one
        place that says whose money is in escrow — so this reads the transaction it
        names rather than searching escrow by metadata, which is exactly the mistake
        the field was added to fix.
        """
        from decimal import Decimal

        from users.models import TrainerClientRelation

        if "_hold_cache" not in self.__dict__:
            relation = (TrainerClientRelation.objects
                        .filter(client_id=obj.owner_id, status='approved')
                        .exclude(hold_reference='')
                        .select_related('trainer').first())
            amount = Decimal("0.00")
            if relation is not None:
                amount = (Transaction.objects
                          .filter(reference_id=relation.hold_reference)
                          .values_list('amount', flat=True).first()) or Decimal("0.00")
            self._hold_cache = (amount, relation)
        return self._hold_cache

    def get_held(self, obj):
        if obj.owner_type != "client":
            return "0.00"
        return str(self._hold(obj)[0])

    def get_held_for(self, obj):
        if obj.owner_type != "client":
            return None
        amount, relation = self._hold(obj)
        if relation is None or not amount:
            return None
        trainer = relation.trainer
        return {"trainer_id": trainer.id,
                "trainer_name": trainer.full_name or trainer.username}

    def get_account_number(self, obj):
        from .utils import account_number_for

        return account_number_for(obj.owner_id)

    # ------------------------------------------- B-096: the trainer's side of escrow
    def _trainer_escrow(self, obj):
        """`users.trainer_escrow.held_for_trainer` for a trainer's wallet, read once per
        response; None for anyone else.

        Decided by the OWNER's role, not `Wallet.owner_type`: the wallet is made by a
        `post_save` on the user's first save, so an account whose role was set after
        that (an operator making someone a trainer) keeps a wallet labelled `client`.
        """
        if getattr(obj.owner, "user_type", None) != "trainer":
            return None
        if "_trainer_escrow_cache" not in self.__dict__:
            from users.trainer_escrow import held_for_trainer

            self._trainer_escrow_cache = held_for_trainer(obj.owner_id, obj.currency)
        return self._trainer_escrow_cache

    def get_held_for_trainer(self, obj):
        escrow = self._trainer_escrow(obj)
        return str(escrow["net"]) if escrow else "0.00"

    def get_held_for_trainer_gross(self, obj):
        escrow = self._trainer_escrow(obj)
        return str(escrow["gross"]) if escrow else "0.00"

    def get_awaiting_first_program(self, obj):
        escrow = self._trainer_escrow(obj)
        return escrow["count"] if escrow else 0

    def get_held_for_trainer_items(self, obj):
        from . import activity

        escrow = self._trainer_escrow(obj)
        if not escrow:
            return []
        when = serializers.DateTimeField()

        def _at(value):
            return when.to_representation(value) if value else None

        return [{
            "relation_id": item["relation_id"],
            "client": item["client"],
            "status": item["status"],
            "kind": item["kind"],
            "reason": activity.reason_of(item["kind"]),
            "month": item["month"],
            "requested_at": _at(item["requested_at"]),
            "held_at": _at(item["held_at"]),
            "amount": str(item["amount"]),
            "commission": str(item["commission"]),
            "commission_rate": format(item["commission_rate"].normalize(), "f"),
            "net": str(item["net"]),
            "currency": item["currency"],
            "charged_amount": str(item["charged_amount"]),
            "charged_currency": item["charged_currency"],
            "estimated": item["estimated"],
        } for item in escrow["items"]]

    # ------------------------- the payouts review, 2026-09-24: a trainer's share owed
    def _trainer_owed(self, obj):
        """`users.trainer_escrow.owed_to_trainer` for a trainer's wallet, read once per
        response; None for anyone else (the owner's role, as `_trainer_escrow`)."""
        if getattr(obj.owner, "user_type", None) != "trainer":
            return None
        if "_trainer_owed_cache" not in self.__dict__:
            from users.trainer_escrow import owed_to_trainer

            self._trainer_owed_cache = owed_to_trainer(obj.owner_id, obj.currency)
        return self._trainer_owed_cache

    def get_owed_to_trainer(self, obj):
        owed = self._trainer_owed(obj)
        return str(owed["net"]) if owed else "0.00"

    def get_owed_to_trainer_items(self, obj):
        from . import activity

        owed = self._trainer_owed(obj)
        if not owed:
            return []
        when = serializers.DateTimeField()

        def _at(value):
            return when.to_representation(value) if value else None

        return [{
            "period_id": item["period_id"],
            "relation_id": item["relation_id"],
            "client": item["client"],
            "status": item["status"],
            "kind": item["kind"],
            "reason": activity.reason_of(item["kind"]),
            "month": item["month"],
            "owed_since": _at(item["owed_since"]),
            "starts_at": _at(item["starts_at"]),
            "amount": str(item["amount"]),
            "commission": str(item["commission"]),
            "commission_rate": format(item["commission_rate"].normalize(), "f"),
            "net": str(item["net"]),
            "currency": item["currency"],
            "charged_amount": str(item["charged_amount"]),
            "charged_currency": item["charged_currency"],
            "estimated": item["estimated"],
        } for item in owed["items"]]


class _TransactionListSerializer(serializers.ListSerializer):
    """Gives the child one batch of counterparties for the whole page.

    DRF renders a list row by row, so anything a field looks up is looked up per row.
    The counterparty is a user the row names in its metadata, and reading it per row
    made the ledger cost a query per row again — the exact regression
    `test_the_ledger_does_not_cost_two_queries_per_row` guards. One query here, before
    the first row is rendered.
    """

    def to_representation(self, data):
        rows = list(data)
        prime = getattr(self.child, "prime_counterparties", None)
        if prime is not None:
            prime(rows)
        # The withdrawals the page's rows name (B-125), in one query too.
        prime_withdrawals = getattr(self.child, "prime_withdrawals", None)
        if prime_withdrawals is not None:
            prime_withdrawals(rows)
        return super().to_representation(rows)


class TransactionSerializer(TransactionWithdrawalFields, TransactionSettlingFields,
                            serializers.ModelSerializer):
    """One row of "الحركات" on the Wallet screen.

    The stored columns say what the database did; `kind`, `reason`, `direction`,
    `signed_amount` and `counterparty` say what HAPPENED, which is what the screen
    draws: an icon, a line of text, a name and a date, and a number with a sign.
    `wallet.activity` derives all five, and the `wallet` in the serializer context is
    whose side the row is read from — the same escrow release is "returned to you"
    for the client and invisible to everyone else.

    Branch on `kind`, never on `reason`: `reason` is translated and may be reworded;
    `kind` is a contract value and will not change.
    """
    source_owner = serializers.SerializerMethodField()
    destination_owner = serializers.SerializerMethodField()
    kind = serializers.SerializerMethodField()
    reason = serializers.SerializerMethodField()
    direction = serializers.SerializerMethodField()
    signed_amount = serializers.SerializerMethodField()
    counterparty = serializers.SerializerMethodField()

    class Meta:
        model = Transaction
        #: Resolves a whole page's counterparties in one query, before any row is
        #: rendered. Per row it was one query each, which is the N+1 the ledger's
        #: own gate test catches (20 rows, 21 extra queries).
        list_serializer_class = _TransactionListSerializer
        fields = [
            "reference_id",
            "tx_type",
            "actor",
            "source_wallet",
            "destination_wallet",
            "amount",
            "currency",
            "money_type",
            "metadata",
            "created_at",
            "source_owner",
            "destination_owner",
            "kind",
            "reason",
            "direction",
            "signed_amount",
            "counterparty",
        ]
        read_only_fields = fields

    @property
    def _wallet(self):
        """Whose ledger this is being read as. Absent for a caller with no wallet in
        context (the operator console's exports), and then the derived fields are
        null rather than a guess about a reader who is not there."""
        return self.context.get("wallet")

    def get_kind(self, obj):
        from . import activity

        wallet = self._wallet
        return activity.kind_of(obj, wallet) if wallet is not None else None

    def get_reason(self, obj):
        from . import activity

        kind = self.get_kind(obj)
        return activity.reason_of(kind) if kind else None

    def get_direction(self, obj):
        from . import activity

        wallet = self._wallet
        return activity.direction_of(obj, wallet) if wallet is not None else None

    def get_signed_amount(self, obj):
        wallet = self._wallet
        if wallet is None:
            return None
        sign = "" if obj.destination_wallet_id == wallet.id else "-"
        return f"{sign}{obj.amount}"

    def prime_counterparties(self, rows):
        """Read every counterparty of `rows` in one query, before rendering starts."""
        from . import activity

        wallet = self._wallet
        if wallet is None:
            return
        owner_ids = self.context.get("ledger_owner_ids")
        ids = {activity.counterparty_id(tx, wallet, owner_ids=owner_ids)
               for tx in rows}
        self.context["counterparty_users"] = activity.counterparty_users(ids)

    def get_counterparty(self, obj):
        from . import activity

        wallet = self._wallet
        if wallet is None:
            return None
        return activity.counterparty_of(
            obj, wallet, owner_ids=self.context.get("ledger_owner_ids"),
            users=self.context.get("counterparty_users"))

    def get_source_owner(self, obj):
        if obj.source_wallet and obj.source_wallet.owner:
            return {
                "id": obj.source_wallet.owner_id,
                "username": getattr(obj.source_wallet.owner, "username", None),
                "user_type": getattr(obj.source_wallet.owner, "user_type", None),
            }
        return None

    def get_destination_owner(self, obj):
        if obj.destination_wallet and obj.destination_wallet.owner:
            return {
                "id": obj.destination_wallet.owner_id,
                "username": getattr(obj.destination_wallet.owner, "username", None),
                "user_type": getattr(obj.destination_wallet.owner, "user_type", None),
            }
        return None


class AgentProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentProfile
        fields = [
            "id", "user", "wallet_type", "daily_limit", "monthly_limit", "daily_limit_usd",
            "monthly_limit_usd", "status", "ip_allowlist",
            "created_at", "updated_at"
        ]
        read_only_fields = ["created_at", "updated_at", "user"]


def _topup_currency_field():
    """The currency of the cash an agent took. REQUIRED, and one the platform trades in.

    A top-up carried no currency, so the typed number was minted in whatever the
    client's wallet held — 500,000 pounds of cash became 500,000 dollars in a wallet
    that had chosen dollars (SIDE_BUGS S-059). There is no default: cash does not come
    labelled, and guessing is the bug.
    """
    from .currencies import CURRENCY_CODES

    return serializers.ChoiceField(choices=list(CURRENCY_CODES))


class TopUpRequestSerializer(serializers.Serializer):
    client_identifier = serializers.CharField()  # id or email
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=_MIN_AMOUNT)
    currency = _topup_currency_field()
    idempotency_key = serializers.CharField()
    #: Signed AND checked for freshness. The view used to check the header's copy and
    #: sign the body's, which made them independent and the replay window unbounded.
    timestamp = serializers.IntegerField()
    #: Declared and read by nothing — the signature the view verifies comes from the
    #: `X-Agent-Auth` header. Kept required so a caller that sends it here and nowhere
    #: else fails loudly at the signature check rather than being quietly unauthenticated.
    signature = serializers.CharField()


class TransferRequestSerializer(serializers.Serializer):
    trainer_id = serializers.IntegerField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=_MIN_AMOUNT)
    idempotency_key = serializers.CharField()


class ReversalRequestSerializer(serializers.Serializer):
    reference_id = serializers.CharField()
    reason = serializers.CharField(required=False, allow_blank=True)
    idempotency_key = serializers.CharField()


class AgentTopUpProxyRequestSerializer(serializers.Serializer):
    client_identifier = serializers.CharField()
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=_MIN_AMOUNT)
    currency = _topup_currency_field()
    idempotency_key = serializers.CharField()


class AgentListingSerializer(serializers.ModelSerializer):
    """An agent as the TopUp screen lists them: a shop to walk into.

    Published fields only, and only for an agent an operator has listed. The account
    behind it — limits, key ids, ip allowlist, the user's own registration phone — is
    not here and must not be added: this is the one agent surface a client can read.
    """
    name = serializers.SerializerMethodField()
    phone = serializers.CharField(source="public_phone", read_only=True)
    hours = serializers.CharField(source="opening_hours", read_only=True)

    class Meta:
        model = AgentProfile
        fields = ["id", "name", "city", "area", "hours", "phone"]
        read_only_fields = fields

    def get_name(self, obj):
        return obj.display_name or getattr(obj.user, "full_name", "") or obj.user.username


class AgentLedgerEntrySerializer(serializers.BaseSerializer):
    """One row of `GET wallet/agent/ledger/` (BACKEND_BUGS B-114). Read-only; the shape
    is `wallet.agents.entry_row`, documented on `AgentLedgerView`."""

    def to_representation(self, instance):
        from .agents import entry_row

        return entry_row(instance)


class AgentSettlementSerializer(serializers.BaseSerializer):
    """One row of `GET wallet/agent/settlements/`. Read-only; `wallet.agents.settlement_row`."""

    def to_representation(self, instance):
        from .agents import settlement_row

        return settlement_row(instance)
