"""The operator console for routine products (registered on `/dj-admin/` in
`admin_dashboard/admin.py`; plan step 10).

Two pages:

* **Routine products** — every listing, the review queue first (`?status=in_review`,
  oldest submission first when filtered). Two actions: **approve** (in review ->
  published; the trainer is pushed `routine_product_approved`) and **reject** (in review
  -> draft with the REASON typed beside the action, which the trainer is shown; pushed
  `routine_product_rejected`). Each goes through `routine/products.py` under a row lock,
  so two operators cannot both decide one product, and each is written to the admin
  log. Approval is refused when the trainer changed the program after submitting it.
* **Routine purchases** — every sale, read-only, with one action: **refund** (the
  reason typed beside it), which reverses every ledger row of the sale and revokes the
  buyer's copy (`routine/products.refund`). Refused, with nothing moved, when the
  trainer's wallet no longer holds their share.

Who: operators only — a superuser or an `admin` account; a bare staff account sees
nothing (as `trainer_verification/admin.py`). A decision also needs the CHANGE
permission on the model, like `TransferReviewAdmin`. Nothing is added or deleted here,
and no field is edited by hand: every change is one of the actions.

Console strings are plain English, like every other page of the console; the words the
trainer and the buyer read (pushes, the API) go through gettext.
"""
from django import forms
from django.contrib import admin, messages
from django.contrib.admin.helpers import ActionForm

from training_platform.admin_utils import log_admin_action

from . import products
from .product_models import ProductStatus, RoutineProduct, RoutinePurchase


def _is_operator(user) -> bool:
    return bool(user and user.is_active
                and (user.is_superuser or getattr(user, "is_admin", False)))


class ReasonActionForm(ActionForm):
    """The action bar with a box for the reason a rejection or a refund needs."""
    reason = forms.CharField(
        required=False, max_length=products.MAX_REASON, label="Reason",
        help_text="Required to reject a product or refund a purchase. The trainer (or "
                  "the record) is shown it.")


class _OperatorOnly(admin.ModelAdmin):
    action_form = ReasonActionForm

    def has_module_permission(self, request):
        return _is_operator(request.user)

    def has_view_permission(self, request, obj=None):
        return _is_operator(request.user)

    def has_change_permission(self, request, obj=None):
        return _is_operator(request.user) and super().has_change_permission(request, obj)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def _run(self, request, queryset, decide, verb):
        done, refused = 0, []
        for row in queryset:
            try:
                decided = decide(row)
            except (products.ReviewConflict, RoutineProduct.DoesNotExist,
                    RoutinePurchase.DoesNotExist) as exc:
                refused.append(f"{row}: {exc}")
                continue
            log_admin_action(request, decided, verb)
            done += 1
        if done:
            self.message_user(request, f"{verb}: {done}.", messages.SUCCESS)
        for line in refused:
            self.message_user(request, line, messages.ERROR)


class RoutineProductAdmin(_OperatorOnly):
    list_select_related = ("trainer", "source_template", "reviewed_by")
    list_display = ("title", "trainer", "status", "price", "currency", "preview_days",
                    "submitted_at", "review_decision", "reviewed_by", "published_at")
    list_filter = ("status", "review_decision", "currency")
    search_fields = ("title_ar", "title_en", "trainer__email", "trainer__username")
    ordering = ("-created_at", "-id")
    readonly_fields = [f.name for f in RoutineProduct._meta.concrete_fields]
    actions = ["approve", "reject"]

    def get_ordering(self, request):
        # The queue: oldest submission first, when an operator filters to it.
        if request.GET.get("status__exact") == ProductStatus.IN_REVIEW:
            return ("submitted_at", "id")
        return self.ordering

    @admin.display(description="Title")
    def title(self, obj):
        return obj.title_ar or obj.title_en or f"#{obj.pk}"

    @admin.action(description="Approve: publish the selected products", permissions=["change"])
    def approve(self, request, queryset):
        self._run(request, queryset, lambda p: products.approve(p.pk, request.user),
                  "Product approved")

    @admin.action(description="Reject: back to draft, with the reason typed above",
                  permissions=["change"])
    def reject(self, request, queryset):
        reason = request.POST.get("reason", "")
        self._run(request, queryset, lambda p: products.reject(p.pk, request.user, reason),
                  f"Product rejected: {reason.strip()}")


class RoutinePurchaseAdmin(_OperatorOnly):
    list_select_related = ("buyer", "product", "refunded_by")
    list_display = ("id", "product", "buyer", "status", "amount", "currency",
                    "commission", "trainer_amount", "trainer_currency", "created_at",
                    "refunded_at")
    list_filter = ("status", "currency", "paid_with")
    search_fields = ("buyer__email", "buyer__username", "product__title_ar",
                     "product__title_en", "charge_reference", "idempotency_key")
    ordering = ("-created_at", "-id")
    readonly_fields = [f.name for f in RoutinePurchase._meta.concrete_fields]
    actions = ["refund"]

    @admin.action(description="Refund: reverse the sale and revoke the buyer's copy, with "
                              "the reason typed above", permissions=["change"])
    def refund(self, request, queryset):
        reason = request.POST.get("reason", "")
        self._run(request, queryset, lambda p: products.refund(p.pk, request.user, reason),
                  f"Purchase refunded: {reason.strip()}")
