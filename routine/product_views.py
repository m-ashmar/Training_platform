"""Routine products on the wire (plan step 10; BACKEND_BUGS B-099). The service is
`routine/products.py`, the shapes `routine/product_serializers.py`.

    trainer   GET, POST  /api/v1/trainer/products/               TrainerProductsView
              GET, PATCH /api/v1/trainer/products/{id}/          TrainerProductDetailView
              POST       /api/v1/trainer/products/{id}/submit/   TrainerProductSubmitView
              POST       /api/v1/trainer/products/{id}/retire/   TrainerProductRetireView
    buyer     GET        /api/v1/products/                       ProductCatalogueView
              GET        /api/v1/products/{id}/                  ProductDetailView
              POST       /api/v1/products/{id}/buy/              ProductBuyView
    operator  approve / reject a product, refund a purchase:     the console, /dj-admin/

These are ROUTINE_PRODUCT endpoints, separate from every coaching route: a routine a
trainer writes for one client inside a coaching is not sold here, and nothing here
touches a coaching (the spec's section 3).

Every refusal names its own `code`; the body is `{error, detail, code}` plus the keys a
row says. Nothing here is cached (`training_platform/cache_config.py` lists none of it),
so an answer that differs by build needs nothing more than the headers it is computed
from.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from policy.money import MoneyType
from policy.services import require_purchase_allowed
from wallet.throttles import ChargingRateThrottle

from . import products
from .product_models import ProductStatus, PurchaseStatus, RoutineProduct, RoutinePurchase
from .product_serializers import (
    BuySerializer, CatalogueProductSerializer, TrainerProductSerializer,
    TrainerProductWriteSerializer, build_may_buy, build_may_show_price,
)

logger = logging.getLogger(__name__)


def _refusal(message, code, http_status, **extra):
    return Response({"error": message, "code": code, **extra}, status=http_status)


#: The words for each service refusal. `code` is the contract; these are for people.
def _refusal_text(code):
    return {
        "product_not_editable": _("This program can only be edited while it is a draft."),
        "product_not_submittable": _("This program cannot be sent for review in its current "
                                     "state."),
        "trainer_not_verified": _("Only verified trainers can publish programs. Your drafts "
                                  "are kept."),
        "product_incomplete": _("Fill in the missing details before sending this program "
                                "for review."),
        "program_too_long": _("This program is too long to sell."),
        "preview_covers_program": _("The free preview cannot cover the whole program."),
        "product_already_retired": _("This program is already retired."),
        "product_not_for_sale": _("This program is not for sale right now."),
        "already_owned": _("You already own this program."),
        "idempotency_key_reused": _("This idempotency key was already used for a different "
                                    "request."),
    }.get(code, _("Request could not be completed."))


def _service_refusal(exc: products.ProductRefusal):
    return Response({"error": _refusal_text(exc.code), "code": exc.code, **exc.extra},
                    status=exc.status)


def _not_found():
    return _refusal(_("Program not found."), "not_found", status.HTTP_404_NOT_FOUND)


def _trainers_only(request):
    if not getattr(request.user, "is_trainer", False):
        return _refusal(_("Only trainers can sell programs."), "not_a_trainer",
                        status.HTTP_403_FORBIDDEN)
    return None


def _refuse_a_price_this_build_may_not_show(request):
    """A price may be SET only from a build that may show one (see the serializers'
    docstring). Raises the policy's own refusal: 400 `app_channel_required` for no
    channel, 403 `not_available_on_channel` for `play` / `ios`."""
    from policy.channels import channel_of
    from policy.exceptions import AppChannelRequired, NotAvailableOnChannel

    if isinstance(request.data, dict) and "price" in request.data \
            and not build_may_show_price(request):
        if channel_of(request) is None:
            raise AppChannelRequired()
        raise NotAvailableOnChannel()


def _with_sales(queryset):
    """Each product's confirmed sales: count, gross (price of record) and net (what
    reached the trainer). A refunded purchase is not a sale."""
    confirmed = Q(purchases__status=PurchaseStatus.CONFIRMED)
    return queryset.annotate(
        sales_count=Count("purchases", filter=confirmed),
        gross=Sum("purchases__price_amount", filter=confirmed),
        net=Sum("purchases__trainer_amount", filter=confirmed))


def _trainer_context(request):
    from wallet.models import currency_for

    return {"request": request, "net_currency": currency_for(request.user)}


def _own_product(request, pk):
    return (_with_sales(RoutineProduct.objects.filter(trainer=request.user, pk=pk))
            .select_related("source_template", "cover").first())


class TrainerProductsView(APIView):
    """`/api/v1/trainer/products/` — the trainer's products (TrainerProducts), and a new
    draft (TrainerProductForm, «حفظ كمسودة»). Trainers only; any trainer may keep
    drafts, only a verified one may submit (`submit/`).

    **GET** — paginated (`?page`, `?page_size` up to 100), newest first:
    `{count, next, previous, results: [product], summary: {published, sales, net,
    net_currency}, commission_rate}`. `summary` is the header of the screen: products
    published, confirmed sales across all of them, and what reached the trainer's
    wallet (`net`, in `net_currency`, the trainer's currency). `commission_rate` is
    today's (`"0.20"`: the trainer keeps the rest — «تحصل على 80% من كل بيع»).

    A `product`: `id`, `status` (`draft` | `in_review` | `published` | `retired`),
    `on_sale`, `template_changed`, `title_ar`, `title_en`, `description_ar`,
    `description_en`, `preview_days`, `template` (`{id, name, days, weeks,
    session_count, program_days}` or null), `cover` (`{asset_id, slot, url_1x, url_2x,
    blur, focal, brightness, alt_ar, alt_en}` or null), `price`, `currency`,
    `net_price` (the price less today's commission: the form's «صافي لك بعد العمولة»),
    `commission_rate`, `sales_count`, `gross`, `gross_currency`, `net`,
    `net_currency`, `review` (`{decision: approved|rejected|null, reason, reviewed_at}`
    — a rejection's reason is the operator's words), `submitted_at`, `published_at`,
    `retired_at`, `created_at`, `updated_at`.

    **POST** — a new DRAFT. Body (JSON): `source_template_id` (required: one of YOUR
    templates), and optionally `title_ar`, `title_en` (≤120), `description_ar`,
    `description_en` (≤2000), `price` (0.01 – 10,000,000, in your currency — the server
    sets `currency`), `preview_days` (0-7), `cover_asset_id` (a picture in your media
    kit, or null). 201 with the product.

    **Differs by build.** A build that may not show a price (`can_show_prices` off:
    `play`, `ios`; or no `X-App-Channel`) is not sent `price`, `currency` or
    `net_price` — the keys are absent — and may not SET a price: a body carrying
    `price` answers 403 `not_available_on_channel` (400 `app_channel_required` with no
    channel). The sales record (`sales_count`, `gross`, `net`, `summary`) is money that
    already moved and is the same on every build.

    | Status | `code` | When |
    |---|---|---|
    | 400 | `validation_error` | a field is malformed; `field_errors.<field>[0].code` is `required` (`source_template_id`), `template_not_yours`, `price_out_of_range`, `preview_days_out_of_range`, `cover_not_yours`, or DRF's own (`max_length`, `invalid`, `max_decimal_places`, ...) |
    | 400 | `app_channel_required` | a `price` was sent with no `X-App-Channel` |
    | 403 | `not_a_trainer` | the caller is not a trainer |
    | 403 | `not_available_on_channel` | a `price` was sent from a build that may not show one |
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        refused = _trainers_only(request)
        if refused:
            return refused
        from users.coaching import commission_rate
        from wallet.models import currency_for

        from .views import StandardResultsSetPagination

        queryset = (_with_sales(RoutineProduct.objects.filter(trainer=request.user))
                    .select_related("source_template", "cover")
                    .order_by("-created_at", "-id"))
        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        context = _trainer_context(request)
        response = paginator.get_paginated_response(
            TrainerProductSerializer(page, many=True, context=context).data)
        currency = currency_for(request.user)
        sales = RoutinePurchase.objects.filter(product__trainer=request.user,
                                               status=PurchaseStatus.CONFIRMED)
        response.data["summary"] = {
            "published": RoutineProduct.objects.filter(
                trainer=request.user, status=ProductStatus.PUBLISHED).count(),
            "sales": sales.count(),
            "net": str(sales.filter(trainer_currency=currency)
                       .aggregate(n=Sum("trainer_amount"))["n"] or "0.00"),
            "net_currency": currency,
        }
        response.data["commission_rate"] = str(commission_rate())
        return response

    def post(self, request):
        refused = _trainers_only(request)
        if refused:
            return refused
        _refuse_a_price_this_build_may_not_show(request)
        from wallet.models import currency_for

        serializer = TrainerProductWriteSerializer(
            data=request.data, context={"request": request, "creating": True})
        serializer.is_valid(raise_exception=True)
        product = serializer.apply(RoutineProduct(trainer=request.user,
                                                  currency=currency_for(request.user)))
        product.save()
        return Response(TrainerProductSerializer(_own_product(request, product.pk),
                                                 context=_trainer_context(request)).data,
                        status=status.HTTP_201_CREATED)


class TrainerProductDetailView(APIView):
    """`/api/v1/trainer/products/{id}/` — one of the trainer's products.

    **GET** — the product (the shape in `TrainerProductsView`).

    **PATCH** — edit a DRAFT; the body is `TrainerProductsView`'s POST, every field
    optional. A product in review, published or retired is not edited here: resubmit
    after changing its template (`submit/`), or retire it. 200 with the product.
    Differs by build exactly as POST does (`price`).

    | Status | `code` | When |
    |---|---|---|
    | 400 | `validation_error` | as POST, `field_errors.<field>[0].code` |
    | 400 | `app_channel_required` | a `price` was sent with no `X-App-Channel` |
    | 403 | `not_a_trainer` | the caller is not a trainer |
    | 403 | `not_available_on_channel` | a `price` was sent from a build that may not show one |
    | 404 | `not_found` | no such product, or not yours |
    | 409 | `product_not_editable` | it is not a draft; `status` says what it is |
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        refused = _trainers_only(request)
        if refused:
            return refused
        product = _own_product(request, pk)
        if product is None:
            return _not_found()
        return Response(TrainerProductSerializer(product,
                                                 context=_trainer_context(request)).data)

    def patch(self, request, pk):
        refused = _trainers_only(request)
        if refused:
            return refused
        _refuse_a_price_this_build_may_not_show(request)
        from wallet.models import currency_for

        serializer = TrainerProductWriteSerializer(data=request.data, partial=True,
                                                   context={"request": request})
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            try:
                product = products.lock_own(request.user, pk)
            except RoutineProduct.DoesNotExist:
                return _not_found()
            if product.status != ProductStatus.DRAFT:
                return _service_refusal(products.ProductNotEditable(status=product.status))
            serializer.apply(product)
            product.currency = currency_for(request.user)
            product.save()
        return Response(TrainerProductSerializer(_own_product(request, pk),
                                                 context=_trainer_context(request)).data)


class TrainerProductSubmitView(APIView):
    """`POST /api/v1/trainer/products/{id}/submit/` — «أرسل للمراجعة»: send a product
    to the operators' review queue. No body. 200 with the product, `status:
    "in_review"`. An operator approves it (`published`, and the trainer is pushed
    `routine_product_approved`) or rejects it with a reason (back to `draft`, pushed
    `routine_product_rejected` with the reason).

    Allowed from `draft`; from `in_review` (the program changed while it waited — the
    review then sees the new version); and from `published` when `template_changed`
    (the product is off sale until the new version is approved). Only a VERIFIED
    trainer may submit; drafts are open to every trainer. Requires both titles, a
    price, a template with exercises, a program of at most 365 training days
    (`template.program_days`), and a free preview shorter than the program. Identical
    on every build (no price is read or sent by it beyond the product's own shape).

    | Status | `code` | When |
    |---|---|---|
    | 403 | `not_a_trainer` | the caller is not a trainer |
    | 403 | `trainer_not_verified` | the trainer's account is not verified (`GET /api/v1/trainer/verification/`) |
    | 404 | `not_found` | no such product, or not yours |
    | 409 | `product_not_submittable` | it is retired, or published with its template unchanged; `status` |
    | 409 | `product_incomplete` | `missing`: any of `title_ar`, `title_en`, `price`, `template`, `template_exercises` |
    | 409 | `program_too_long` | `max_days` (365), `program_days` |
    | 409 | `preview_covers_program` | `preview_days` is not shorter than the program; `program_days` |
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        refused = _trainers_only(request)
        if refused:
            return refused
        try:
            products.submit(request.user, pk)
        except RoutineProduct.DoesNotExist:
            return _not_found()
        except products.ProductRefusal as exc:
            return _service_refusal(exc)
        return Response(TrainerProductSerializer(_own_product(request, pk),
                                                 context=_trainer_context(request)).data)


class TrainerProductRetireView(APIView):
    """`POST /api/v1/trainer/products/{id}/retire/` — take a product off sale for good,
    from any state. No body. 200 with the product, `status: "retired"`. Everyone who
    bought it keeps their copy. Identical on every build.

    | Status | `code` | When |
    |---|---|---|
    | 403 | `not_a_trainer` | the caller is not a trainer |
    | 404 | `not_found` | no such product, or not yours |
    | 409 | `product_already_retired` | it is retired already |
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        refused = _trainers_only(request)
        if refused:
            return refused
        try:
            products.retire(request.user, pk)
        except RoutineProduct.DoesNotExist:
            return _not_found()
        except products.ProductRefusal as exc:
            return _service_refusal(exc)
        return Response(TrainerProductSerializer(_own_product(request, pk),
                                                 context=_trainer_context(request)).data)


def _buyer_context(request, product_list, **extra):
    """What every product on the page needs, read once: which of them the reader owns
    (and their copies), the reader's currency and — only when some price has to cross
    — the operator's rate, and whether this build may buy at all."""
    from wallet.exchange import current_rate
    from wallet.models import currency_for

    ids = [p.pk for p in product_list]
    owned = {}
    if ids:
        from .models import Routine

        purchases = dict(RoutinePurchase.objects.filter(
            buyer=request.user, product_id__in=ids, status=PurchaseStatus.CONFIRMED)
            .values_list("pk", "product_id"))
        copies = dict(Routine.objects.filter(purchase_id__in=list(purchases))
                      .values_list("purchase_id", "pk"))
        owned = {product_id: copies.get(purchase_id)
                 for purchase_id, product_id in purchases.items()}
    context = {"request": request, "owned": owned, "may_buy": build_may_buy(request),
               **extra}
    if build_may_show_price(request):
        reader_currency = currency_for(request.user)
        context["reader_currency"] = reader_currency
        if any(p.currency != reader_currency for p in product_list):
            context["rate"] = current_rate()
    return context


class ProductCatalogueView(APIView):
    """`GET /api/v1/products/` — the programs for sale. Signed-in callers, any role.

    Paginated (`?page`, `?page_size` up to 100), newest listing first; `?trainer_id=`
    narrows to one trainer. Lists products that are published and still backed by the
    version of the program their review saw, by a trainer who is publicly visible and
    has not blocked (or been blocked by) the caller.

    A row: `id`, `title` / `description` (in the request's language, the other one
    when that is empty), `title_ar`, `title_en`, `description_ar`, `description_en`,
    `trainer` (`{id, name}`), `days` (training days a week), `weeks`,
    `session_count` (`days` x `weeks`, null while the trainer has not said),
    `preview_days`, `cover` (as the trainer's route, or null), `owned`, `routine_id`
    (the caller's copy when `owned`), `can_buy`, `published_at`; and on a build that
    may show a price: `price` and `currency` IN THE READER'S CURRENCY, with
    `price_converted`, `exchange_rate`, `price_of_record`, `price_of_record_currency`
    — the plans' shape (`subscription.pricing.Priced`).

    **Differs by build.** `play`, `ios` or no channel: no price key at all, and
    `can_buy` is false — the spec's "not sold in app": no price beside a call to
    action. Listing and reading work everywhere; buying only on `apk` and `web`.

    | Status | `code` | When |
    |---|---|---|
    | 400 | `bad_trainer_id` | `trainer_id` is not a whole number |
    | 400 | `validation_error` | a bad `page` / `page_size` (`field_errors`) |
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .views import StandardResultsSetPagination

        queryset = (products.catalogue_for(request.user)
                    .select_related("trainer", "source_template", "cover")
                    .order_by("-published_at", "-id"))
        raw = request.query_params.get("trainer_id")
        if raw not in (None, ""):
            try:
                queryset = queryset.filter(trainer_id=int(str(raw).strip()))
            except ValueError:
                return _refusal(_("trainer_id must be a whole number."), "bad_trainer_id",
                                status.HTTP_400_BAD_REQUEST)
        paginator = StandardResultsSetPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        page = list(page or [])
        return paginator.get_paginated_response(CatalogueProductSerializer(
            page, many=True, context=_buyer_context(request, page)).data)


class ProductDetailView(APIView):
    """`GET /api/v1/products/{id}/` — one program, with its free preview.

    The row of `GET /api/v1/products/` plus `preview`: the first `preview_days` days of
    the program as the buyer's copy will hold them — `[{day, exercises: [{exercise_id,
    name, target_muscle, sets, reps, rest_time, order}]}]` (empty when `preview_days`
    is 0). Readable by anyone it is offered to, by a buyer who owns it (a retired one
    too — their copy is `routine_id`), and by its trainer. The preview is content, not
    a price, so it is the same on every build; prices and `can_buy` differ exactly as
    in the catalogue.

    | Status | `code` | When |
    |---|---|---|
    | 404 | `not_found` | no such product, or not one the caller may read |
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        product = products.visible_product(request.user, pk)
        if product is None:
            return _not_found()
        context = _buyer_context(request, [product], preview=products.preview_of(product))
        return Response(CatalogueProductSerializer(product, context=context).data)


class ProductBuyView(APIView):
    """`POST /api/v1/products/{id}/buy/` — buy a program from the wallet. Clients only.

    Headers: `X-App-Channel` (and `X-App-Version`). Body (JSON): `idempotency_key`
    (1-64 characters, the app's own for this purchase). One atomic step: the whole
    price leaves the buyer's wallet, the trainer is paid their share and the platform
    its commission (`COACHING_COMMISSION_RATE`, snapshotted on the purchase), and the
    buyer's own copy of the program is written and assigned to them — see
    `routine/products.py`. A price in the other currency is converted at the
    operator's rate, which is snapshotted too. The same key again answers the same
    purchase (200, nothing moves twice).

    200: `{purchase: {id, product_id, status: "confirmed", money_type:
    "ROUTINE_PRODUCT", amount, currency,
    price_of_record, price_of_record_currency, exchange_rate, routine_id,
    charge_reference, created_at}}` — `routine_id` is the buyer's copy (read and train
    it with the routine routes, on every build); `charge_reference` is the wallet row
    (`GET /api/wallet/transactions/{reference_id}/`). The buyer is pushed
    `routine_product_purchased`, the trainer `routine_product_sold`.

    **Differs by build.** Only `apk` and `web` may buy: `play` and `ios` are refused
    in code before anything is read (the spec: ROUTINE_PRODUCT is not sold in app).

    | Status | `code` | When |
    |---|---|---|
    | 400 | `app_channel_required` | no known `X-App-Channel` |
    | 403 | `not_available_on_channel` | this build may not buy programs (`play`, `ios`) |
    | 400 | `validation_error` | `idempotency_key` missing or longer than 64 (`field_errors`) |
    | 404 | `not_found` | no such product, or not one offered to the caller — a draft, one in review, a retired one, or one whose program changed since its review (an owner or its trainer gets the rows below instead) |
    | 403 | `own_product` | the caller wrote it |
    | 403 | `clients_only` | the caller is not a client (the code the coaching and transfer routes answer for the same fact) |
    | 409 | `already_owned` | a confirmed purchase exists; `purchase_id`, `routine_id` |
    | 409 | `product_not_for_sale` | it stopped being on sale while this request ran (retired, or its program changed) |
    | 409 | `exchange_rate_missing` / `exchange_rate_stale` | the price has to be converted and the operator's rate is missing or too old |
    | 402 | `insufficient_balance` | the wallet holds less than the price; `amount`, `currency` |
    | 422 | `idempotency_key_reused` | the key already bought a different product |
    | 400 | `purchase_refused` | the ledger refused the movement for another reason; nothing moved |
    | 429 | `throttled` | the rate limiter (`Retry-After`) |
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [ChargingRateThrottle]

    def post(self, request, pk):
        # FIRST: a build that may not buy programs is told so whatever it sent
        # (policy/services.py). `play` and `ios` never get past this line.
        require_purchase_allowed(request, MoneyType.ROUTINE_PRODUCT)

        serializer = BuySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        product = products.visible_product(request.user, pk)
        if product is None:
            return _not_found()
        if product.trainer_id == request.user.pk:
            return _refusal(_("You cannot buy your own program."), "own_product",
                            status.HTTP_403_FORBIDDEN)
        if not getattr(request.user, "is_client", False):
            return _refusal(_("Only clients can buy programs."), "clients_only",
                            status.HTTP_403_FORBIDDEN)

        from wallet.exchange import ExchangeRateUnavailable, refusal_body
        from wallet.models import InsufficientBalance

        try:
            purchase, _created = products.buy(
                product.pk, request.user,
                idempotency_key=serializer.validated_data["idempotency_key"])
        except products.ProductRefusal as exc:
            return _service_refusal(exc)
        except ExchangeRateUnavailable as exc:
            return Response(refusal_body(exc), status=status.HTTP_409_CONFLICT)
        except InsufficientBalance:
            from subscription.pricing import price_for
            from wallet.models import currency_for

            currency = currency_for(request.user)
            needed = price_for(product.price, product.currency, currency)
            return _refusal(_("Insufficient wallet balance."), "insufficient_balance",
                            status.HTTP_402_PAYMENT_REQUIRED,
                            amount=str(needed.amount), currency=needed.currency)
        except ValueError:
            logger.exception("Routine product %s: the ledger refused a purchase", product.pk)
            return _refusal(_("Request could not be completed."), "purchase_refused",
                            status.HTTP_400_BAD_REQUEST)
        return Response({"purchase": purchase_payload(purchase)})


def purchase_payload(purchase) -> dict:
    """The buyer's receipt of one purchase (`ProductBuyView`)."""
    from .models import Routine

    routine_id = Routine.objects.filter(purchase=purchase).values_list("pk", flat=True).first()
    converted = purchase.exchange_rate_id is not None
    return {
        "id": purchase.pk,
        "product_id": purchase.product_id,
        "status": purchase.status,
        "money_type": purchase.money_type,
        "amount": str(purchase.amount),
        "currency": purchase.currency,
        "price_of_record": str(purchase.price_amount) if converted else None,
        "price_of_record_currency": purchase.price_currency if converted else None,
        "exchange_rate": (str(purchase.exchange_rate_value)
                          if purchase.exchange_rate_value is not None else None),
        "routine_id": routine_id,
        "charge_reference": purchase.charge_reference,
        "created_at": purchase.created_at,
    }
