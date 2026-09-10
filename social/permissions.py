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
    """Reads follow the queryset; MODIFYING the object requires owning it.

    `owner_fields` covers the differing names across the social models
    (`author` on Post/Comment, `creator` on Challenge).

    "Modifying" is not the same as "a non-safe method". Liking a post, liking a
    comment, joining a challenge and logging progress are all POSTs to a detail route,
    and every one of them creates a row BESIDE the object rather than changing it —
    that is the entire point of a social product. Treating them as writes meant
    `get_object()` refused everyone but the author: the like button, the comment-like
    button, Join and Log progress returned 403 for every user on every piece of content
    they did not write, and the only case that worked was liking your own post.

    A viewset opts an action out by name in `interaction_actions`. Opt-in rather than
    opt-out, so a new action that really does modify the object is refused by default.
    """

    owner_fields = ('author', 'creator', 'user', 'owner')

    def has_permission(self, request, view):
        # Authentication is required for every method, safe ones included.
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True
        if getattr(view, 'action', None) in getattr(view, 'interaction_actions', ()):
            # Acting ON someone's content, not editing it. Whether the actor may SEE
            # the object is the queryset's job and is enforced before this runs.
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
