"""
Object-level authorization for the social app.

The viewsets here are full ModelViewSets whose read querysets deliberately include
other people's public content (that is what a feed is). Without an object-level
check, DRF grants write access to everything that read query returns — any
authenticated user could PATCH or DELETE any other user's post, comment or
challenge. Queryset scoping is never sufficient on its own for write methods.
"""

from rest_framework.permissions import BasePermission, SAFE_METHODS


class IsOwnerOrReadOnly(BasePermission):
    """Reads follow the queryset; writes require owning the object.

    `owner_fields` covers the differing names across the social models
    (`author` on Post/Comment, `creator` on Challenge).
    """

    owner_fields = ('author', 'creator', 'user', 'owner')

    def has_permission(self, request, view):
        # Authentication is required for every method, safe ones included.
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if getattr(user, 'is_admin', False) or user.is_staff:
            return True
        for field in self.owner_fields:
            if hasattr(obj, f'{field}_id'):
                return getattr(obj, f'{field}_id') == user.id
        return False


class IsFollowParticipant(BasePermission):
    """A follow edge may only be modified by the follower who created it."""

    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if getattr(user, 'is_admin', False) or user.is_staff:
            return True
        return obj.follower_id == user.id
