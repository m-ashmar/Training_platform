"""The receipt for a payment that is already made — as data, and as a PDF.

A receipt is a RECORD, not a sale. Nothing here opens a checkout, quotes a price for
something not yet bought, or tells anyone how to pay, so it is not gated by
`policy.services.require_purchase_allowed` and it is answered identically on every
`X-App-Channel`. See `subscription.views.PaymentReceiptView` for that argument in full.

The number on it is the invoice number `PaymentService` mints when a payment completes
(`PaymentService.ensure_invoice_number` — there is exactly one generator, and it is
there). This module never invents one.

Arabic is the primary language of the document
------------------------------------------------
A PDF has no text engine: whatever bytes it is given are drawn glyph by glyph, left to
right, in the order they appear. Arabic therefore needs two steps done BEFORE the text
reaches the page, and a font that has the glyphs they produce:

1. `arabic_reshaper` replaces each letter with its joining form (initial, medial,
   final, isolated) from the Arabic Presentation Forms block.
2. `python-bidi` applies the Unicode bidirectional algorithm, which puts a mixed line
   such as "Premium · 30 يوماً" into the visual order a reader expects. Doing only
   step 1 gives joined letters running backwards; doing neither gives "ﺍ ﺏ ﺕ" — the
   detached letters people describe as "the PDF came out broken".
3. The font is Noto Naskh Arabic, shipped in `subscription/fonts/` under the SIL Open
   Font License (`OFL.txt`). It is here because coverage of the PRESENTATION FORMS
   step 1 produces is what decides whether the receipt renders or shows boxes, and
   most UI Arabic fonts do not have them: measured on this receipt's own strings, the
   app's brand font (Alexandria) is missing 14 of the forms it needs and Cairo the
   same 14, while Noto Naskh Arabic and Amiri have them all. Noto is the smaller of
   the two (200 KB, instanced to a single static weight) and covers the Latin and the
   digits the same document needs.

Why `fpdf2` and not one of the usual answers: it is pure Python and ~240 KB with no
system libraries (WeasyPrint needs cairo and pango in the image; reportlab is a C
extension an order of magnitude larger), it embeds and subsets a TrueType font, and
with the two steps above it is the whole dependency. The three packages together are
the smallest thing that renders this document correctly in Arabic.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.utils.translation import pgettext

from wallet.currencies import RATE_BASE, RATE_QUOTE

#: The one font the document is drawn with. See the module docstring.
FONT_PATH = Path(__file__).resolve().parent / "fonts" / "NotoNaskhArabic-Regular.ttf"

#: The page: ISO 216 A5, portrait, in millimetres.
A5_MM = (148, 210)

#: `Payment.payment_method` -> what the receipt calls it. A method with no entry is
#: shown by its stored code rather than by a guess.
METHOD_NAMES = {
    "shamcash": gettext_lazy("ShamCash"),
    "manual": gettext_lazy("Recorded by the platform"),
    "syriatel_cash": gettext_lazy("Syriatel Cash"),
    "baraka_bank": gettext_lazy("Al-Baraka Bank"),
    "bemo_bank": gettext_lazy("BEMO Bank"),
}


def method_name(code: str) -> str:
    label = METHOD_NAMES.get(code)
    return str(label) if label is not None else code


def platform_details() -> dict:
    """Who issued the receipt. Settings, never a literal in a template.

    The spec requires the platform's legal name on every receipt (§7). The trading
    name is a TRANSLATED string, not a setting's literal, so the Arabic receipt says
    "يلا جيم" — a settings value is read once at import and could only ever be one
    language. `PLATFORM_NAME` overrides it for a deployment that trades under another
    name. `legal_name` falls back to the trading name so a deployment that has not
    registered one still produces a complete document; `contact_email` and `website`
    are empty until someone sets them, and are then absent from the page rather than
    printed blank.
    """
    name = getattr(settings, "PLATFORM_NAME", "") or _("Yalla Gym")
    return {
        "name": str(name),
        "legal_name": getattr(settings, "PLATFORM_LEGAL_NAME", "") or str(name),
        "contact_email": getattr(settings, "PLATFORM_CONTACT_EMAIL", ""),
        "website": getattr(settings, "PLATFORM_WEBSITE", ""),
    }


def item_of(payment) -> dict:
    """What was bought, in the pieces the app draws as "Premium · 30 يوماً".

    A routine product's web-hub checkout bought a program, not a plan: its item adds
    `product_id` and `product_title`, and its plan keys are empty (BACKEND_BUGS B-167).
    A plan's item is exactly what it always was."""
    # What THIS payment bought, from its own snapshot: the subscription may be on
    # another plan by the time the receipt is reprinted (`Payment.plan_bought`).
    item = {**payment.plan_bought(), "money_type": payment.money_type}
    product = payment.product_bought()
    if product is not None:
        item.update(product)
    return item


def item_line(item) -> str:
    """The item as one line of the PDF and the email: "Premium · 30 days", or the
    program's title (a product has no length: it used to read " · None days")."""
    if item.get("product_id") is not None:
        return str(item.get("product_title") or "")
    if item.get("duration_days"):
        return f"{item['plan_name']} · {item['duration_days']} " + _("days")
    return str(item.get("plan_name") or "")


def upgrade_of(payment) -> dict | None:
    """What an upgrade charged and why it is less than the plan's price, or None.

    Read from the record the checkout kept (`metadata['upgrade']`,
    `subscription.upgrade.UpgradeQuote.as_record`): the plan it replaced, the new plan's
    price, the credit for the whole days left on the old one, and what was paid. A
    receipt for 14,000 on a 20,000 plan must say where the other 6,000 went.
    """
    from .models import _snapshot_name

    if (payment.metadata or {}).get("kind") != "upgrade":
        return None
    record = (payment.metadata or {}).get("upgrade") or {}
    from_plan = record.get("from_plan") or {}
    # What the SWITCH did when the money cleared (`PaymentService._apply_upgrade`), not
    # what the quote said: the credit is re-read then. Late — no cover left to credit —
    # means no credit was applied at all, so none is shown (the review, U15).
    switch = record.get("switch") or {}
    late = bool(switch.get("late"))
    return {
        "from_plan_id": from_plan.get("id"),
        "from_plan_name": _snapshot_name(from_plan),
        "new_price": record.get("new_price"),
        "credit": None if late else switch.get("credit_applied", record.get("credit")),
        "days_left": None if late else switch.get("days_credited", record.get("days_left")),
        "granted_days": switch.get("granted_days"),
        # What the granted days were worth: what was paid plus the credit. It is the
        # plan's price when the full length was granted, and always reconciles —
        # `period_price - credit == amount` (the review, L14).
        "period_price": str((Decimal(payment.amount)
                             + Decimal(switch.get("credit_applied") or "0"))
                            .quantize(Decimal("0.01"))),
        "currency": record.get("currency") or payment.currency,
        "late": late,
    }


def issued_at(payment):
    """When the money was confirmed.

    `completed_at` is stamped once, by `PaymentService`, and never moves again. Older
    rows completed before that column existed fall back to `updated_at`, which is the
    best evidence those rows carry.
    """
    return payment.completed_at or payment.updated_at


def exchange_of(payment) -> dict | None:
    """What this charge was converted from, and at what rate — or None.

    Read entirely from the payment's own columns. A receipt that recomputed the rate
    would say something different every time the operator set a new one.
    """
    if not payment.exchange_rate_value or not payment.price_currency:
        return None
    if payment.price_currency == payment.currency:
        return None
    return {
        "price_of_record": str(payment.price_amount),
        "price_of_record_currency": payment.price_currency,
        "rate": str(payment.exchange_rate_value),
        # Written out, so nobody has to know which way round the pair is stored.
        "rate_text": f"1 {payment.price_currency} = {payment.exchange_rate_value} "
                     f"{payment.currency}"
        if payment.price_currency == RATE_BASE else
        f"1 {RATE_BASE} = {payment.exchange_rate_value} {RATE_QUOTE}",
    }


#: `EmailDelivery.status` -> what the receipt says of its email. `sending` is still on
#: its way, as far as the reader is concerned.
EMAIL_STATES = {"queued": "queued", "sending": "queued", "sent": "sent",
                "failed": "failed", "skipped": "skipped"}


def receipt_email(payment) -> dict:
    """Whether the receipt email for this payment left (BACKEND_BUGS B-171):
    `{"status": "sent" | "queued" | "failed" | "skipped" | "none", "sent_at": <when> |
    null}`.

    Read from the one `EmailDelivery` the receipt email is owed under
    (`payment_receipt:<payment id>`, `notifications.channels.email.queue_receipt`).
    `queued`: owed and not sent yet (the worker sends it shortly); `sent`: it left, at
    `sent_at`; `skipped`: the worker did not send it (the account could no longer be
    emailed, or muted it in between); `failed`: the mail server refused it for good, or
    every retry failed; `none`: it was never owed — the person's notification
    preferences stop email for `payment_confirmed`, the address is not verified, or the
    payment was confirmed before receipts were emailed. Only `sent` means "the receipt
    is in your inbox". The same on every build; never on the PDF."""
    from notifications.models import EmailDelivery

    row = (EmailDelivery.objects
           .filter(recipient_id=payment.payer_id, dedup_key=f"payment_receipt:{payment.pk}")
           .values("status", "sent_at").first())
    if row is None:
        return {"status": "none", "sent_at": None}
    state = EMAIL_STATES.get(row["status"], "queued")
    return {"status": state, "sent_at": row["sent_at"] if state == "sent" else None}


def receipt_data(payment, *, invoice_number: str | None = None) -> dict:
    """The receipt for one completed payment, as data. The app draws it.

    `invoice_number` is passed in by the caller that minted it, so the value on the
    JSON and the value on the PDF are one read of one row.

    `amount`/`currency` are WHAT WAS CHARGED, in the currency the payer trades in.
    When the plan was priced in the other one, `exchange` says so: the price of record,
    its currency, and the rate this charge was struck at — read off the payment's own
    snapshot (`Payment.exchange_rate_value`), never from today's rate. The pound moves,
    and a receipt reprinted next month must still say what happened this month.
    `exchange` is None for a same-currency purchase, which is most of them.
    """
    item = item_of(payment)
    # The subscriber, or a routine product checkout's buyer (B-167): never
    # `payment.subscription.user`, which a product's checkout does not have.
    payer = payment.payer
    return {
        "payment_id": str(payment.id),
        "invoice_number": invoice_number or payment.invoice_number,
        "issued_at": issued_at(payment),
        "status": "paid",
        "item": item,
        "amount": str(payment.amount),
        "currency": payment.currency,
        "exchange": exchange_of(payment),
        "upgrade": upgrade_of(payment),
        "method": {"code": payment.payment_method,
                   "name": method_name(payment.payment_method)},
        "reference": payment.gateway_transaction_reference or "",
        "covers": {"from": payment.covers_from, "until": payment.covers_until},
        "payer": {"id": payer.id,
                  "name": (payer.full_name or payer.username),
                  "email": payer.email},
        "platform": platform_details(),
        "plan_duration_days": item["duration_days"],
    }


# --------------------------------------------------------------------------- PDF
def _shape(text: str) -> str:
    """Logical text -> the visually ordered, joined string a PDF can draw.

    Both steps run for every string, Arabic or not: `python-bidi` leaves a pure Latin
    run alone, and the alternative — deciding per string whether to shape it — is how
    a mixed label ends up half-reordered.
    """
    import arabic_reshaper
    from bidi.algorithm import get_display

    return get_display(arabic_reshaper.reshape(str(text)))


def missing_glyphs(text: str) -> set[str]:
    """Characters of `text` the receipt font cannot draw, AFTER shaping.

    A PDF that references a glyph the font does not have draws nothing, or a box, and
    still reports as a valid PDF — so "it rendered" is not evidence that it is
    readable. This is what the test asserts on, and what a new string added to the
    document should be checked against.
    """
    from fontTools.ttLib import TTFont

    cmap = TTFont(str(FONT_PATH), fontNumber=0).getBestCmap()
    return {ch for ch in _shape(text) if ord(ch) not in cmap}


def _money(amount, currency: str) -> str:
    return f"{Decimal(amount):,.2f} {currency}"


def render_pdf(data: dict, *, compress: bool = True) -> bytes:
    """The same receipt as a one-page A5 PDF, in the active language.

    `compress=False` leaves the content streams and the `/ToUnicode` map readable in
    the output, which is how the test reads the text back out of the document.
    """
    from fpdf import FPDF

    # ISO A5 as millimetres, not the name "A5": fpdf2 2.8.5 corrected its A5 width
    # (148.5 mm to 148 mm) and warns on every use of the name to say so. These are the
    # corrected dimensions, stated where they cannot move again.
    pdf = FPDF(format=A5_MM, unit="mm")
    pdf.set_compression(compress)
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_font("receipt", "", str(FONT_PATH))
    pdf.set_font("receipt", size=11)
    pdf.set_margins(12, 12, 12)
    pdf.add_page()
    # The document reads right to left, so every line is laid out from the right.
    pdf.set_right_margin(12)

    platform = data["platform"]
    pdf.set_font("receipt", size=16)
    pdf.cell(0, 10, _shape(platform["name"]), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("receipt", size=9)
    if platform["legal_name"] != platform["name"]:
        pdf.cell(0, 5, _shape(platform["legal_name"]), align="R",
                 new_x="LMARGIN", new_y="NEXT")
    for line in (platform["contact_email"], platform["website"]):
        if line:
            pdf.cell(0, 5, _shape(line), align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)
    pdf.set_font("receipt", size=14)
    pdf.cell(0, 9, _shape(_("Payment receipt")), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("receipt", size=20)
    pdf.cell(0, 12, _shape(_money(data["amount"], data["currency"])), align="R",
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("receipt", size=11)
    pdf.cell(0, 7, _shape(pgettext("receipt status", "Paid")), align="R",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    item = data["item"]
    issued = timezone.localtime(data["issued_at"]) if data["issued_at"] else None
    # A converted purchase says so ON THE DOCUMENT. A customer who sees a pound figure
    # for a plan they know is priced in dollars must be able to read why, and at what
    # rate, without asking anyone.
    fx = data.get("exchange")
    rows = [
        (_("Invoice number"), data["invoice_number"] or ""),
        (_("Date"), issued.strftime("%Y-%m-%d %H:%M") if issued else ""),
        (_("Item"), item_line(item)),
        (_("Payment method"), data["method"]["name"]),
        (_("Payment reference"), data["reference"]),
        (_("Customer"), data["payer"]["name"]),
    ]
    if fx:
        rows[3:3] = [
            (_("Price"), _money(fx["price_of_record"], fx["price_of_record_currency"])),
            (_("Exchange rate"), fx["rate_text"]),
        ]
    up = data.get("upgrade")
    if up and up.get("credit") is not None and Decimal(up["credit"]) > 0:
        # An upgrade charged the new plan's price LESS the credit for the days left on
        # the plan it replaced: both are on the document, so the amount explains itself.
        rows[3:3] = [
            (_("Upgraded from"), up["from_plan_name"]),
            (_("Plan price"), _money(up["period_price"], up["currency"])),
            (_("Credit for days left"), "-" + _money(up["credit"], up["currency"])),
        ]
    for label, value in rows:
        if not str(value):
            continue
        # Value on the left, label on the right: the reading order of the screen.
        pdf.cell(52, 8, _shape(value), align="L")
        pdf.cell(0, 8, _shape(label), align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)
    pdf.set_font("receipt", size=8)
    pdf.multi_cell(0, 5, _shape(
        _("This receipt was issued electronically and needs no signature.")),
        align="R")

    return bytes(pdf.output())
