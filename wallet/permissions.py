from rest_framework.permissions import BasePermission


class IsAgent(BasePermission):
    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        return bool(user and user.is_authenticated and getattr(user, "user_type", None) == "agent")


class IsAdmin(BasePermission):
    def has_permission(self, request, view):
        user = getattr(request, "user", None)
        return bool(user and user.is_authenticated and getattr(user, "is_superuser", False))


class IsOwner(BasePermission):
    def has_object_permission(self, request, view, obj):
        user = getattr(request, "user", None)
        if hasattr(obj, "owner_id"):
            return bool(user and user.is_authenticated and obj.owner_id == user.id)
        return False


