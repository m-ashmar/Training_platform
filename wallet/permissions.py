
from rest_framework.permissions import BasePermission


class IsAgent(BasePermission):
    """The agent endpoints (top-ups and API keys). Anyone else: 403 `agents_only`.

    `code` is what DRF hands to `PermissionDenied`, and the exception handler answers it
    as the top-level `code` — the refusal used to answer only the 403 default,
    `permission_denied` (the owner, 2026-09-24: every refusal names its own code).
    """
    code = "agents_only"

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        return bool(user and user.is_authenticated and getattr(user, "user_type", None) == "agent")


class IsAdmin(BasePermission):
    """The operator endpoints (reversal, audit export, alerts). Anyone else: 403
    `admins_only` — the same code, for the same meaning, as the subscription app's
    operator endpoint (the owner, 2026-09-24)."""
    code = "admins_only"

    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        return bool(user and user.is_authenticated and getattr(user, "is_superuser", False))


class IsOwner(BasePermission):
    def has_object_permission(self, request, view, obj):
        user = getattr(request, "user", None)
        if hasattr(obj, "owner_id"):
            return bool(user and user.is_authenticated and obj.owner_id == user.id)
        return False
