"""The operator console's Media Kit: upload to a slot, set the focal point and alt text,
and see which trainers' kits are still missing slots.

Uploads are validated and graded exactly as `mediakit.services.store` does it; a refused
file shows its reason on the form instead of saving anything.
"""
from django import forms
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.template.response import TemplateResponse
from django.urls import path
from django.utils.html import format_html

from .models import MediaAsset
from .processing import process_upload
from .services import apply_upload, kit_status, resolve_owner
from .slots import SLOTS, OwnerType, required_keys, resolve


def _slot_help():
    rows = []
    for s in SLOTS.values():
        key = f"{s.key}.N (1-{s.max_index})" if s.max_index else s.key
        size = "x".join(str(v) for v in s.min_size) if s.width_1x else "-"
        rows.append(f"{key} [{s.owner_type}, {s.kind}, {s.ratio[0]}:{s.ratio[1]}, min {size}]")
    return "; ".join(rows)


class MediaAssetForm(forms.ModelForm):
    upload = forms.FileField(required=False,
                             help_text="JPEG, PNG or WebP. Required for a new slot.")

    class Meta:
        model = MediaAsset
        fields = ["owner_type", "owner_id", "slot", "focal_x", "focal_y", "alt_ar", "alt_en"]
        help_texts = {"slot": _slot_help(),
                      "owner_id": "The trainer's user id, the exercise, program or dish "
                                  "id. Ignored for app content.",
                      "focal_x": "0 = left edge, 1 = right edge.",
                      "focal_y": "0 = top edge, 1 = bottom edge."}

    def clean(self):
        cleaned = super().clean()
        # Once saved, the owner and slot are read-only (MediaAssetAdmin) and come from
        # the row: moving a picture to another slot would skip that slot's checks.
        existing = self.instance.pk is not None
        owner_type = self.instance.owner_type if existing else cleaned.get("owner_type")
        slot_key = self.instance.slot if existing else cleaned.get("slot")
        owner_id = self.instance.owner_id if existing else (cleaned.get("owner_id") or 0)
        if not owner_type or not slot_key:
            return cleaned
        try:
            owner_id, self.person = resolve_owner(owner_type, owner_id, slot_key)
        except ValidationError as exc:
            raise ValidationError({"upload" if existing else "slot": exc.messages})
        if not existing:
            cleaned["owner_id"] = owner_id
        upload = cleaned.get("upload")
        self.processed = None
        if upload:
            from training_platform.api_exceptions import UploadBusy

            try:
                self.processed = process_upload(upload, resolve(slot_key))
            except ValidationError as exc:
                raise ValidationError({"upload": exc.messages})
            except UploadBusy as busy:
                # Every decode slot is taken (`image_budget.decode_slot`): the console
                # says so on the form instead of answering a 500.
                raise ValidationError({"upload": [str(busy.detail)]})
        elif not self.instance.pk:
            raise ValidationError({"upload": "A new slot needs an image."})
        return cleaned

    def _post_clean(self):
        # The model's own checks (slot, person) run after `person` is known — and only
        # once `clean()` resolved it; a failed clean leaves the row's person as it was.
        if hasattr(self, "person"):
            self.instance.person = self.person
        super()._post_clean()


class MediaAssetAdmin(admin.ModelAdmin):
    form = MediaAssetForm
    list_display = ("owner_type", "owner_id", "slot", "preview", "brightness", "updated_at")
    list_filter = ("owner_type", "slot")
    search_fields = ("slot",)
    readonly_fields = ("preview", "brightness", "width", "height", "uploaded_by",
                       "created_at", "updated_at")

    @admin.display(description="Preview")
    def preview(self, obj):
        if not obj or not obj.file_1x:
            return "-"
        return format_html('<img src="{}" style="max-height:120px;max-width:200px">',
                           obj.file_1x.url)

    def get_readonly_fields(self, request, obj=None):
        fields = tuple(super().get_readonly_fields(request, obj))
        return fields + ("owner_type", "owner_id", "slot") if obj is not None else fields

    def save_model(self, request, obj, form, change):
        obj.person = getattr(form, "person", obj.person)
        obj.uploaded_by = request.user
        if getattr(form, "processed", None) is not None:
            apply_upload(obj, form.processed)
        obj.save()

    def get_urls(self):
        return [path("kits/", self.admin_site.admin_view(self.kits_view),
                     name="mediakit_trainer_kits")] + super().get_urls()

    def kits_view(self, request):
        """Every trainer with a completeness meter and the slots still missing."""
        from users.models import CustomUser

        filled = {}
        for owner_id, slot in (MediaAsset.objects.filter(owner_type=OwnerType.TRAINER)
                               .values_list("owner_id", "slot")):
            filled.setdefault(owner_id, set()).add(slot)
        rows = []
        for trainer in (CustomUser.objects.filter(user_type="trainer", is_active=True)
                        .order_by("id")):
            status = kit_status(OwnerType.TRAINER, filled.get(trainer.pk, ()))
            rows.append({"trainer": trainer, **status})
        context = {**self.admin_site.each_context(request),
                   "title": "Trainer media kits", "rows": rows,
                   "required": required_keys(OwnerType.TRAINER)}
        return TemplateResponse(request, "mediakit/trainer_kits.html", context)
