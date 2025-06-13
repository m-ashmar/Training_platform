from rest_framework.permissions import BasePermission, SAFE_METHODS


class IsAdminOrOwnerOrReadOnly(BasePermission):
    """
    Custom permission to allow:
    - Only admin users (is_staff) to create, update, or delete objects.
    - All users to have read-only access (GET, HEAD, OPTIONS).
    """

    def has_permission(self, request, view):
        # Allow safe methods (GET, HEAD, OPTIONS) for all users
        if request.method in SAFE_METHODS:
            return True

        # Allow modifying methods (POST, PUT, PATCH, DELETE) only for admin users
        return request.user and request.user.is_staff

    def has_object_permission(self, request, view, obj):
        # Allow safe methods for all users
        if request.method in SAFE_METHODS:
            return True

        # Allow modifying methods only for admin users
        return request.user and request.user.is_staff