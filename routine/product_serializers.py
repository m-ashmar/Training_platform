"""What the product endpoints answer (`routine/product_views.py`), and the one input.

PRICES AND THE BUILD. ROUTINE_PRODUCT is store-billed money, so a build whose
`can_show_prices` is off (`play`, `ios`, or a request that declares no channel) is
never SENT a product's price — to a buyer or to the trainer who set it: the keys are
absent, not null (`policy.services.can_show_price`, the rule plan prices follow). What
stays on every build is what is not an offer: the listing's content, the free preview,
and — for the trainer — the RECORD of sales already made (count, gross, net), which is
money that moved, like the wallet's ledger. `can_buy` is false on those builds, always.
"""
from __future__ import annotations

from decimal import Decimal

from django.utils.translation import get_language
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from policy.money import MoneyType
from wallet.exchange import READ_THE_RATE

from .product_models import MAX_PREVIEW_DAYS, MAX_PRICE, ProductPlace, RoutineProduct

#: The keys the trainer's own product carries only on a build that may show a price:
#: the price they set and its net. (A buyer's product is given its price keys —
#: `subscription.pricing.Priced.as_payload` — only on such a build.)
TRAINER_PRICE_FIELDS = ("price", "currency", "net_price")


def build_may_show_price(request) -> bool:
    """`can_show_price(request, ROUTINE_PRODUCT)`, asked once per request."""
    from policy.services import can_show_price

    memo = getattr(request, "_product_price_memo", None)
    if memo is None:
        memo = can_show_price(request, MoneyType.ROUTINE_PRODUCT)
        try:
            request._product_price_memo = memo
        except AttributeError:
            pass
    return memo


def build_may_buy(request) -> bool:
    """Whether this build may buy a product at all (`require_purchase_allowed`)."""
    from policy.exceptions import AppChannelRequired, NotAvailableOnChannel
    from policy.services import require_purchase_allowed

    try:
        require_purchase_allowed(request, MoneyType.ROUTINE_PRODUCT)
    except (AppChannelRequired, NotAvailableOnChannel):
        return False
    return True


def _cover(product, request):
    from mediakit.services import payload

    asset = product.cover
    if asset is None:
        return None
    return {"asset_id": asset.pk, "slot": asset.slot, **payload(asset, request)}


def _template(product):
    from .products import program_days

    template = product.source_template
    if template is None:
        return None
    return {"id": template.pk, "name": template.name, "days": template.days,
            "weeks": template.weeks, "session_count": template.session_count,
            "program_days": program_days(template)}


def _money(value):
    return None if value is None else str(Decimal(value).quantize(Decimal("0.01")))


def _goal(product):
    """The program's goal from the closed list (`ProductGoal`: `build_muscle`,
    `strength`, `fat_loss`, `rehab`), read from its template's goal
    (`routine.products.goal_code`), or None when that says none of them."""
    from .products import goal_code

    template = product.source_template
    return goal_code(template.goal) if template is not None else None


class TrainerProductSerializer(serializers.Serializer):
    """The trainer's own product (TrainerProducts, TrainerProductForm).

    `net_price` is the price less the commission at today's rate (`commission_rate`,
    the same `users.coaching.split` a sale uses); a sale snapshots the rate of its
    moment. `sales_count`, `gross` (in `currency` of the price of record, as
    `gross_currency`) and `net` (what reached the trainer, in `net_currency`) count
    confirmed purchases only — a refunded one is taken out. `on_sale` is false while
    the template has changed since the review saw it (`template_changed`), until the
    trainer submits it again.
    """

    def to_representation(self, product):
        from users.coaching import commission_rate

        from .products import net_of

        request = self.context.get("request")
        rate = commission_rate()
        _commission, net = net_of(product.price, rate)
        data = {
            "id": product.pk,
            "status": product.status,
            "on_sale": product.is_on_sale,
            "template_changed": product.template_changed,
            "title_ar": product.title_ar,
            "title_en": product.title_en,
            "description_ar": product.description_ar,
            "description_en": product.description_en,
            "preview_days": product.preview_days,
            "goal": _goal(product),
            "place": product.place or None,
            "template": _template(product),
            "cover": _cover(product, request),
            "price": _money(product.price),
            "currency": product.currency,
            "net_price": _money(net),
            "commission_rate": str(rate),
            "sales_count": getattr(product, "sales_count", None) or 0,
            "gross": _money(getattr(product, "gross", None) or 0),
            "gross_currency": product.currency,
            "net": _money(getattr(product, "net", None) or 0),
            "net_currency": self.context.get("net_currency"),
            "review": {"decision": product.review_decision or None,
                       "reason": product.review_reason or None,
                       "reviewed_at": product.reviewed_at},
            "submitted_at": product.submitted_at,
            "published_at": product.published_at,
            "retired_at": product.retired_at,
            "created_at": product.created_at,
            "updated_at": product.updated_at,
        }
        if request is not None and not build_may_show_price(request):
            for key in TRAINER_PRICE_FIELDS:
                data.pop(key, None)
        return data


class CatalogueProductSerializer(serializers.Serializer):
    """A product as a buyer reads it. See the module docstring for what a store build
    is not sent. `owned` / `routine_id`: the reader bought it, and `routine_id` is
    their copy (an ordinary routine of theirs, on every build). `can_buy`: this build
    may buy it, the reader is a client, it is on sale and not already theirs.
    `buyers`: the product's `buyers_count` (the view annotates or sets it) as
    `routine.products.buyers_shown` shows it — null below five."""

    def to_representation(self, product):
        from subscription.pricing import price_for

        from .products import buyers_shown

        request = self.context.get("request")
        user = getattr(request, "user", None)
        language = get_language() or "ar"
        owned = self.context.get("owned", {})
        template = product.source_template
        data = {
            "id": product.pk,
            "title": product.title_in(language),
            "title_ar": product.title_ar,
            "title_en": product.title_en,
            "description": product.description_in(language),
            "description_ar": product.description_ar,
            "description_en": product.description_en,
            "trainer": {"id": product.trainer_id,
                        "name": (product.trainer.full_name or product.trainer.username)},
            "days": template.days if template else None,
            "weeks": template.weeks if template else None,
            "session_count": template.session_count if template else None,
            "preview_days": product.preview_days,
            "goal": _goal(product),
            "place": product.place or None,
            "buyers": buyers_shown(getattr(product, "buyers_count", None)),
            "cover": _cover(product, request),
            "owned": product.pk in owned,
            "routine_id": owned.get(product.pk),
            "published_at": product.published_at,
        }
        may_show = request is not None and build_may_show_price(request)
        if may_show and product.price is not None:
            reader_currency = self.context.get("reader_currency") or product.currency
            priced = price_for(product.price, product.currency, reader_currency,
                               rate=self.context.get("rate", READ_THE_RATE))
            data.update(priced.as_payload())
        data["can_buy"] = bool(
            may_show and self.context.get("may_buy") and product.is_on_sale
            and product.pk not in owned and getattr(user, "is_client", False)
            and product.trainer_id != getattr(user, "pk", None))
        if "preview" in self.context:
            data["preview"] = self.context["preview"]
        return data


class BuySerializer(serializers.Serializer):
    """`idempotency_key`: the app's key for this purchase, 1-64 characters. A retry
    with the same key answers the same purchase. A key starting `checkout:` (in any
    case) is the server's own — a checkout's delivery writes one — and is refused with
    the field code `idempotency_key_reserved` (SIDE_BUGS S-234)."""
    idempotency_key = serializers.CharField(max_length=64, min_length=1)

    def validate_idempotency_key(self, value):
        from .products import CHECKOUT_KEY_PREFIX

        if value.lower().startswith(CHECKOUT_KEY_PREFIX):
            raise serializers.ValidationError(
                _("This key is reserved. Send the key your app made for this purchase."),
                code="idempotency_key_reserved")
        return value


class TrainerProductWriteSerializer(serializers.Serializer):
    """The trainer's draft. Every field optional on an edit; `source_template_id` is
    required to create one. `place` is `home`, `gym`, or `""` / null for "not said".
    Field codes (`field_errors.<field>[0].code`): `template_not_yours`,
    `price_out_of_range`, `preview_days_out_of_range`, `cover_not_yours`, `bad_place`,
    and DRF's own (`required`, `max_length`, `invalid`, ...)."""
    source_template_id = serializers.IntegerField(required=False)
    title_ar = serializers.CharField(max_length=120, required=False, allow_blank=True)
    title_en = serializers.CharField(max_length=120, required=False, allow_blank=True)
    description_ar = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    description_en = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    price = serializers.DecimalField(max_digits=12, decimal_places=2, required=False,
                                     allow_null=True)
    preview_days = serializers.IntegerField(required=False)
    cover_asset_id = serializers.IntegerField(required=False, allow_null=True)
    place = serializers.CharField(max_length=16, required=False, allow_blank=True,
                                  allow_null=True)

    def _trainer(self):
        return self.context["request"].user

    def validate(self, attrs):
        if self.context.get("creating") and "source_template_id" not in attrs:
            raise serializers.ValidationError({"source_template_id": [serializers.ErrorDetail(
                _("Choose the template this program is sold from."), code="required")]})
        return attrs

    def validate_source_template_id(self, value):
        from .models import RoutineTemplate

        template = RoutineTemplate.objects.filter(pk=value, created_by=self._trainer()).first()
        if template is None:
            raise serializers.ValidationError(_("Choose one of your own templates."),
                                              code="template_not_yours")
        return template

    def validate_price(self, value):
        if value is not None and not Decimal("0.01") <= value <= MAX_PRICE:
            raise serializers.ValidationError(
                _("The price must be more than zero and at most %(max)s.") % {"max": MAX_PRICE},
                code="price_out_of_range")
        return value

    def validate_preview_days(self, value):
        if not 0 <= value <= MAX_PREVIEW_DAYS:
            raise serializers.ValidationError(
                _("Free preview days must be between 0 and %(max)s.") % {"max": MAX_PREVIEW_DAYS},
                code="preview_days_out_of_range")
        return value

    def validate_place(self, value):
        value = (value or "").strip()
        if value and value not in ProductPlace.values:
            raise serializers.ValidationError(
                _("Choose where the program is trained: home or gym."), code="bad_place")
        return value

    def validate_cover_asset_id(self, value):
        from mediakit.models import MediaAsset
        from mediakit.slots import IMAGE

        if value is None:
            return None
        asset = MediaAsset.objects.filter(pk=value, person=self._trainer()).first()
        if asset is None or asset.kind != IMAGE:
            raise serializers.ValidationError(_("Choose a picture from your own media kit."),
                                              code="cover_not_yours")
        return asset

    def apply(self, product: RoutineProduct) -> RoutineProduct:
        """Copy the validated values onto `product` (not saved)."""
        data = self.validated_data
        if "source_template_id" in data:
            product.source_template = data["source_template_id"]
        for name in ("title_ar", "title_en", "description_ar", "description_en", "price",
                     "preview_days", "place"):
            if name in data:
                value = data[name]
                setattr(product, name, value.strip() if isinstance(value, str) else value)
        if "cover_asset_id" in data:
            product.cover = data["cover_asset_id"]
        return product
