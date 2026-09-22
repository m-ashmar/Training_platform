"""The currencies Yalla Gym trades in, and the rules that bind them.

The owner (2026-09-21): the platform sells in **Syrian pounds AND US dollars**, and in
nothing else. Both are first class. Neither is a leftover: a plan may be priced in
either, a coaching month may be held and paid out in either, and the platform's own
ledger holds a balance in each.

THE FOUR RULES. They are small, and every currency bug this codebase has had is one of
them broken.

1. **A person has ONE currency, and it is their wallet's.** They choose it while the
   wallet is still empty; once it holds a balance or has a single ledger leg it is a
   fact and cannot be changed (`wallet.models.wallet_for`, `Wallet.currency`). From then
   on every price they are shown and every charge they pay is in it — "we force him to
   it" (the owner). There is no second mechanism for "the user's currency": it is
   `Wallet.currency` and nothing else.

2. **A price of record keeps the currency the operator set it in.** A plan priced in USD
   stays a USD plan. What changes is the CHARGE: at purchase the price is converted into
   the buyer's currency at the live rate, and both the converted amount and the rate row
   used are written onto the payment. So every service is buyable in either currency
   (the owner) without the catalogue being duplicated.

3. **The LEDGER never mixes.** A wallet holds one currency for ever and a transaction is
   in one currency. Money that genuinely has to cross — the payout to a trainer who
   holds the other currency — is an explicit two-leg exchange
   (`wallet.models.exchange_funds_atomic`): a debit in one currency and a credit in the
   other, both naming the rate row they used. Never a silent 1:1, and never a conversion
   hidden inside `move_funds_atomic`.

4. **Conversion happens in exactly ONE function**,
   `wallet.exchange.convert_at_the_operators_rate`, at a rate an operator set and that is
   snapshotted onto whatever it touches. The rate is not a constant: the pound moves, so
   an operator maintains it in the console, every rate is a new append-only row, and a
   rate nobody has refreshed lately REFUSES a conversion rather than guessing
   (`wallet.exchange`). A SAME-currency purchase never consults the rate at all and is
   therefore never blocked by a stale one.

Adding a third currency is: an entry here, a migration for the `choices` on the columns
that use this list, a rate row against each existing currency, per-currency gateway
bounds (`subscription/settings/gateway_config.py: amount_limits`), and nothing else —
the platform's ledger owners are derived from this list and appear the first time money
in that currency arrives.
"""

#: `(code, name)`, in the shape Django's `choices` wants. A list, not a tuple, because
#: it is deconstructed into the migrations exactly as written.
SUPPORTED_CURRENCIES = [
    ('SYP', 'Syrian Pound'),
    ('USD', 'US Dollar'),
]

#: Just the codes, for membership tests and for walking the set.
CURRENCY_CODES = tuple(code for code, _label in SUPPORTED_CURRENCIES)

#: The pair a rate is quoted for, written `1 <BASE> = rate <QUOTE>` — "1 usd = x sp"
#: (the owner, 2026-09-21). One pair, in one direction, so there is exactly one number
#: for an operator to maintain and exactly one place a sign can be wrong.
RATE_BASE, RATE_QUOTE = 'USD', 'SYP'


def is_supported(currency) -> bool:
    """True when `currency` is one the platform trades in."""
    return currency in CURRENCY_CODES
