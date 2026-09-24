"""The operator console for trainer verification (registered on `/dj-admin/` in
`admin_dashboard/admin.py`).

Two pages:

* **Verification queue** — the pending applications, oldest submission first. Opening
  one shows the review page.
* **Verification applications** — every application in any state, with its history;
  a verified one can be revoked from its page.

**The review page** replaces the usual change form, because nothing here is edited as
a field: it shows each current document through a short-lived signed URL (five minutes,
`storage.py`), the documents the trainer replaced with what was decided about them, and
every earlier decision; and it carries the three decisions — approve, reject (with a
reason the trainer is shown, and optionally a reason per document), and revoke a
verification. Each goes through `services.decide` / `services.revoke`, under row locks,
and is written to the admin log. The page sends the ids of the documents it showed, so
a decision taken while the trainer was replacing a file is refused, not applied to a
file nobody looked at.

**Who may do it.** Identity documents: operators only — a superuser or an `admin`
account — and a bare staff account gets 403 on every page (as `moderation/admin.py`).
Deciding also needs the change permission on applications, like `TransferReviewAdmin`.
Nothing is added or deleted here: an application is the trainer's, and a decision is
never deleted.

Console strings are plain English, like every other page of the console; the words the
trainer reads (the push, the API) go through gettext.
"""
from django.contrib import admin, messages
from django.contrib.admin.utils import unquote
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.utils import timezone

from training_platform.admin_utils import log_admin_action

from . import services, slots
from .models import (ApplicationState, VerificationApplication, VerificationQueueEntry)
from .storage import signed_url, url_ttl

CHANGE_PERMISSION = "trainer_verification.change_verificationapplication"


def _is_operator(user) -> bool:
    """An actual admin, not merely a staff account."""
    return bool(user and user.is_active
                and (user.is_superuser or getattr(user, "is_admin", False)))


class _ReviewAdmin(admin.ModelAdmin):
    list_select_related = ("trainer", "decided_by")
    list_display = ("trainer_name", "state", "round", "submitted_at", "waiting",
                    "decided_at", "decided_by")
    list_filter = ("state",)
    search_fields = ("trainer__email", "trainer__username", "trainer__first_name",
                     "trainer__last_name")
    ordering = ("submitted_at", "id")
    actions = None

    # --- who ------------------------------------------------------------------
    def has_module_permission(self, request):
        return _is_operator(request.user)

    def has_view_permission(self, request, obj=None):
        return _is_operator(request.user)

    def has_change_permission(self, request, obj=None):
        return _is_operator(request.user) and request.user.has_perm(CHANGE_PERMISSION)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    # --- the list -------------------------------------------------------------
    @admin.display(description="Trainer", ordering="trainer__email")
    def trainer_name(self, obj):
        trainer = obj.trainer
        name = f"{trainer.first_name} {trainer.last_name}".strip()
        return f"{name} <{trainer.email}>" if name else trainer.email

    @admin.display(description="Waiting")
    def waiting(self, obj):
        if obj.state != ApplicationState.PENDING or not obj.submitted_at:
            return "-"
        hours = int((timezone.now() - obj.submitted_at).total_seconds() // 3600)
        return f"{hours // 24}d {hours % 24}h"

    # --- the review page ------------------------------------------------------
    def change_view(self, request, object_id, form_url="", extra_context=None):
        application = self.get_object(request, unquote(object_id))
        if application is None:
            return self._get_obj_does_not_exist_redirect(request, self.opts, object_id)
        if not self.has_view_permission(request, application):
            raise PermissionDenied
        if request.method == "POST":
            if not self.has_change_permission(request, application):
                raise PermissionDenied
            self._decide(request, application)
            return HttpResponseRedirect(request.path)
        return TemplateResponse(request, "admin/trainer_verification/review.html",
                                self._context(request, application))

    def _decide(self, request, application):
        action = request.POST.get("action", "")
        reason = request.POST.get("reason", "")
        seen = [int(x) for x in request.POST.get("seen", "").split(",")
                if x.strip().isdigit()]
        document_reasons = {
            int(key[len("doc_reason_"):]): value
            for key, value in request.POST.items()
            if key.startswith("doc_reason_") and key[len("doc_reason_"):].isdigit()}
        try:
            if action in ("approve", "reject"):
                decision = services.decide(
                    application.pk, request.user, approve=(action == "approve"),
                    reason=reason, document_reasons=document_reasons,
                    seen_document_ids=seen)
            elif action == "revoke":
                decision = services.revoke(application.pk, request.user, reason)
            else:
                self.message_user(request, "Choose approve, reject or revoke.",
                                  messages.ERROR)
                return
        except services.ReviewConflict as exc:
            self.message_user(request, str(exc), messages.ERROR)
            return
        log_admin_action(request, application,
                         f"Verification {decision.outcome} (round {decision.round})"
                         + (f": {decision.reason}" if decision.reason else ""))
        self.message_user(request, f"Verification {decision.outcome}.", messages.SUCCESS)

    def _context(self, request, application):
        documents = list(application.documents.order_by("slot", "-uploaded_at", "-id"))
        order = {slot.key: i for i, slot in enumerate(slots.SLOTS)}
        current = sorted((d for d in documents if d.replaced_at is None),
                         key=lambda d: order.get(d.slot, 99))
        replaced = [d for d in documents if d.replaced_at is not None]
        labels = {slot.key: str(slot.label) for slot in slots.SLOTS}

        def row(doc):
            return {"doc": doc, "label": labels.get(doc.slot, doc.slot),
                    "file_url": signed_url(doc.file, request),
                    "thumbnail_url": signed_url(doc.thumbnail, request)}

        present = {d.slot for d in current}
        can_change = self.has_change_permission(request, application)
        return {
            **self.admin_site.each_context(request),
            "opts": self.opts,
            "title": f"Verification: {self.trainer_name(application)}",
            "application": application,
            "trainer": application.trainer,
            "current": [row(d) for d in current],
            "missing": [labels[k] for k in slots.REQUIRED if k not in present],
            "replaced": [row(d) for d in replaced],
            "decisions": list(application.decisions.select_related("decided_by")),
            "seen": ",".join(str(d.pk) for d in current),
            "can_decide": can_change and application.state == ApplicationState.PENDING,
            "can_revoke": can_change and application.state == ApplicationState.VERIFIED,
            "url_ttl_minutes": max(1, url_ttl() // 60),
            "max_reason": services.MAX_REASON,
        }


class VerificationApplicationAdmin(_ReviewAdmin):
    """Every application, any state."""
    ordering = ("-updated_at", "-id")


class VerificationQueueAdmin(_ReviewAdmin):
    """The pending applications, oldest submission first."""
    list_filter = ()

    def get_queryset(self, request):
        return (super().get_queryset(request)
                .filter(state=ApplicationState.PENDING).order_by("submitted_at", "id"))


# The default site too, as `moderation/admin.py` does; the console registers the same
# classes on `/dj-admin/`.
admin.site.register(VerificationQueueEntry, VerificationQueueAdmin)
admin.site.register(VerificationApplication, VerificationApplicationAdmin)
