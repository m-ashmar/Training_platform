"""Who may act on whom.

Every class here is WIRED to a view. Three were not: `IsClientOrTrainerOrAdmin`,
`IsTrainerForClient` (named in a routine/views.py comment as though it were enforcing
something, and enforcing nothing) and `IsExerciseOwnerOrGlobal`, which returned True for
any authenticated caller on an exercise with no creator — for WRITES as well as reads, so
wiring it to the exercise viewset would have let any client edit or delete every global
exercise in the library. Unused permission code reads as vetted and is not.

SOURCE OF TRUTH: an approved `TrainerClientRelation`, read through
`can_access_user_data` / `accessible_user_ids` below. Nothing in this module may test
`CustomUser.assigned_trainer` — that column is a denormalised pointer for display, and
when the two disagreed it was the pointer that granted access without consent.
"""
from django.db.models import Q
from rest_framework.permissions import BasePermission, SAFE_METHODS


class IsAdminOrOwnerOrReadOnly(BasePermission):
    """
    Custom permission to allow:
    - Only admin users (is_staff) or trainers to create, update, or delete objects.
    - Trainers can only modify their own objects.
    - All users have read-only access (GET, HEAD, OPTIONS).
    """

    def has_permission(self, request, view):
        # Safe methods still require authentication. Returning True unconditionally let
        # anonymous callers past the permission check and into get_queryset, which assumes
        # a real user -> AttributeError -> 500 instead of 401. The crash was the only thing
        # preventing an anonymous read, since has_object_permission also returned True.
        if request.method in SAFE_METHODS:
            return bool(request.user and request.user.is_authenticated)
        # Only trainers and admins can create, update, or delete objects
        return request.user.is_authenticated and (request.user.is_trainer or request.user.is_staff)

    def has_object_permission(self, request, view, obj):
        # Object-level reads are bounded by get_queryset; anonymous is still refused here.
        if request.method in SAFE_METHODS:
            return bool(request.user and request.user.is_authenticated)
        # Allow modifying methods for admins
        if request.user and request.user.is_staff:
            return True
        # Allow modifying methods for trainers if they own the object
        if request.user.is_authenticated and request.user.is_trainer:
            return owner_of(obj) == request.user
        return False


def owner_of(obj):
    """The trainer who owns this row, following the parent when the row has no owner.

    `hasattr(obj, 'created_by')` was the test, and a `RoutineExercise` has no such
    field — its owner is `routine.created_by`. So the check fell through to False and
    a trainer could add an exercise to their own routine and then never edit it again.
    """
    owner = getattr(obj, 'created_by', None)
    if owner is not None:
        return owner
    for parent in ('routine', 'template', 'routine_template'):
        related = getattr(obj, parent, None)
        if related is not None:
            return getattr(related, 'created_by', None)
    return None


class IsTrainerOrAdmin(BasePermission):
    """
    Permission to allow only trainers and admins to access.

    Object-level too. This class defined only `has_permission`, so DRF's default
    `has_object_permission` returned True for every object it was asked about: any
    trainer could PATCH or DELETE any other trainer's routine that happened to reach
    their queryset — which one does as soon as a client moves between trainers.
    """

    def has_permission(self, request, view):
        return request.user.is_authenticated and (request.user.is_trainer or request.user.is_admin)

    def has_object_permission(self, request, view, obj):
        if request.user.is_admin or request.user.is_staff:
            return True
        owner = owner_of(obj)
        if owner is not None:
            return owner == request.user
        # No owner to compare against: fall back to the queryset's scoping for reads,
        # and refuse writes rather than guess.
        return request.method in SAFE_METHODS


class IsClientOrAssignedTrainer(BasePermission):
    """
    Permission to allow clients and their assigned trainers to access data.
    """
    
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated
    
    def has_object_permission(self, request, view, obj):
        user = request.user
        
        # Admins can access everything
        if user.is_admin:
            return True
        
        # For user objects
        if hasattr(obj, 'user_type'):
            if obj.user_type == 'client':
                return user == obj or can_access_user_data(user, obj.id)
            elif obj.user_type == 'trainer':
                return user == obj
        
        # For objects with user field
        if hasattr(obj, 'user'):
            if obj.user.user_type == 'client':
                return user == obj.user or can_access_user_data(user, obj.user_id)
            else:
                return user == obj.user
        
        # For objects with assigned_to field (like routines)
        if hasattr(obj, 'assigned_to'):
            return user in obj.assigned_to.all()
        
        return False


class IsRoutineOwnerOrAssigned(BasePermission):
    """
    Permission for routines - allows creators and assigned users to access.
    """
    
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated
    
    def has_object_permission(self, request, view, obj):
        user = request.user
        
        # Admins can access everything
        if user.is_admin:
            return True
        
        # Creators can access their routines
        if hasattr(obj, 'created_by') and user == obj.created_by:
            return True
        
        # Assigned users can access routines assigned to them
        if hasattr(obj, 'assigned_to') and user in obj.assigned_to.all():
            return True
        
        # Trainers can access routines assigned to their clients
        if user.is_trainer and hasattr(obj, 'assigned_to'):
            return any(can_access_user_data(user, cid)
                   for cid in obj.assigned_to.values_list('id', flat=True))
        
        return False


class IsTrainerOfApprovedClient(BasePermission):
    """
    Enhanced permission that allows access only if the user is a trainer 
    and has an approved TrainerClientRelation with the client.
    
    This permission is used for:
    - Viewing client profiles (read-only)
    - Assigning routines to approved clients
    - Accessing client-specific data
    
    TODO: Add caching for performance optimization
    TODO: Consider adding audit logging for access attempts
    TODO: Implement rate limiting for profile access
    """
    def has_permission(self, request, view):
        # Only trainers and admins can access this endpoint
        return request.user.is_authenticated and (request.user.is_trainer or request.user.is_admin)

    def has_object_permission(self, request, view, obj):
        # obj is expected to be a CustomUser (client)
        from users.models import TrainerClientRelation
        user = request.user
        
        # Admins can access everything
        if user.is_admin:
            return True
            
        # Ensure user is a trainer
        if not user.is_trainer:
            return False
            
        # Check if the object is a client
        if not hasattr(obj, 'user_type') or obj.user_type != 'client':
            return False
            
        # Check for approved trainer-client relationship
        return TrainerClientRelation.objects.filter(
            trainer=user, 
            client=obj, 
            status='approved'
        ).exists()


class IsTrainerOrAdminForAssignment(BasePermission):
    """
    Permission specifically for routine assignment operations.
    Only trainers and admins can assign routines to clients.
    
    This permission ensures that:
    - Only trainers and admins can assign routines
    - Clients cannot assign or edit routines
    - Assignment is restricted to approved client relationships
    
    TODO: Add validation for assignment limits per client
    TODO: Implement assignment scheduling and conflicts checking
    """
    
    def has_permission(self, request, view):
        # Only trainers and admins can assign routines
        return request.user.is_authenticated and (request.user.is_trainer or request.user.is_admin)
    
    def has_object_permission(self, request, view, obj):
        user = request.user
        
        # Admins can assign to anyone
        if user.is_admin:
            return True
            
        # Only trainers can assign routines
        if not user.is_trainer:
            return False
            
        # For routine objects, check if user is the creator
        if hasattr(obj, 'created_by'):
            return user == obj.created_by
            
        return False 


class IsSetLogCreatorOrTrainerOrAdmin(BasePermission):
    """
    Custom permission for ExerciseSetLogViewSet:
    - Allow POST (create) for any authenticated user (clients can log their own sets)
    - Allow update/delete for the person whose set it is, and for admins
    - Read-only for others

    The name is the historical one (it is in `API_CONTRACT.md` against every set-log
    route); the rule it enforces changed on 2026-09-20, in two directions at once.

    The OWNER may now correct and delete their own set (BACKEND_DELTA item 15). They
    could not, which is the wrong way round: they are the only person who knows what
    they lifted, and a mistyped weight was permanent — it fed their volume, their
    records, their one-rep-max estimates and their coach's screen for ever.

    A TRAINER may no longer edit or delete a client's set. They did not lift it. A
    coach who could rewrite the log could also rewrite the record it produced and the
    adherence figure their own work is judged by, and the client has no way to see it
    happen. A coach corrects a client by talking to them; if a set is wrong, the
    client deletes it. Reads are unchanged — the trainer sees everything they saw.

    Admin keeps the write, as the operator of last resort (`/dj-admin/` support work).
    """
    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return bool(request.user and request.user.is_authenticated)
        # Who may write THIS row is an object question, and answering it here as well
        # only decided it for the wrong person: the class-level test read the caller's
        # ROLE, so the owner of a set was refused before the object was ever loaded.
        return bool(request.user and request.user.is_authenticated)

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return bool(request.user and request.user.is_authenticated)
        if request.method == 'POST':
            return bool(request.user and request.user.is_authenticated)
        user = request.user
        if not (user and user.is_authenticated):
            return False
        if user.is_staff or getattr(user, 'is_admin', False):
            return True
        progress = getattr(obj, 'user_exercise_progress', None)
        if progress is not None:
            return progress.user_id == user.id
        # A set with no progress row behind it (an old row): fall back to the session's
        # owner, and refuse when there is nothing to attribute it to.
        session = getattr(obj, 'workout_session', None)
        return session is not None and session.user_id == user.id


class IsSessionOwnerOrTrainerOrAdmin(BasePermission):
    """Who may read and change a WORKOUT SESSION that already exists.

    - GET: the session's owner, their approved trainer, or an admin.
    - PATCH/PUT/DELETE: the session's OWNER and an admin — never their trainer
      (SIDE_BUGS S-067). The rule for a set is that a trainer may no longer edit or
      delete a client's set because they did not lift it, and a session is the sets
      inside it: DELETE cascades to every set logged in it, so a trainer who could
      delete the session could delete the sets the set rule refuses them, and a PATCH
      let them finish (close) a client's day. The trainer still READS everything.

    **This class does not decide POST.** `WorkoutSessionViewSet.get_permissions`
    routes every POST to `IsRoutineOwnerOrAssigned` — a client starting their own
    workout is the ordinary case, and what actually has to be checked is the routine.
    Both methods here carried a POST branch refusing anyone who is not a trainer or
    staff, and it had never run: it read like the rule and said the opposite of it, so
    the next person to trust it would have concluded clients cannot start a workout.
    Removed rather than corrected — the rule for a create belongs in the one class
    that enforces it.
    """
    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return request.user and request.user.is_authenticated
        # PATCH/PUT/DELETE: who may change THIS session is an object question, settled
        # in has_object_permission below.
        return request.user and request.user.is_authenticated

    def has_object_permission(self, request, view, obj):
        # Reads: the session owner, an approved trainer, or admin. Returning True
        # unconditionally for SAFE_METHODS (the previous behaviour) let any
        # authenticated user read any user's workout session by id.
        if request.method in SAFE_METHODS:
            return can_access_user_data(request.user, obj.user_id)
        if request.method in ['PATCH', 'PUT', 'DELETE']:
            # The person who trained, or an admin (the operator of last resort, as for
            # sets). An approved trainer used to pass here through
            # `can_access_user_data` — see the class docstring for what that allowed.
            user = request.user
            if not (user and user.is_authenticated):
                return False
            if user.is_staff or getattr(user, 'is_admin', False):
                return True
            return obj.user_id == user.id
        return False

# ============================================================================
# Shared user-data authorization
# ============================================================================
def can_access_user_data(requester, target_user_id) -> bool:
    """
    Single source of truth for "may `requester` read another user's training data?"

    Allowed when the requester is:
      * the target user themselves,
      * an admin,
      * a trainer with an APPROVED TrainerClientRelation to the target.

    Any endpoint that accepts a caller-supplied user id (?user_id=, ?user=, a path
    id) MUST gate on this. Several routine analytics endpoints previously used the
    supplied id directly, which let any authenticated account read any other user's
    complete training history.
    """
    if requester is None or not getattr(requester, 'is_authenticated', False):
        return False
    if target_user_id in (None, ''):
        return False
    try:
        target_user_id = int(target_user_id)
    except (TypeError, ValueError):
        return False

    if requester.id == target_user_id:
        return True
    if getattr(requester, 'is_admin', False):
        return True
    if getattr(requester, 'is_trainer', False):
        from users.models import TrainerClientRelation
        return TrainerClientRelation.objects.filter(
            trainer=requester, client_id=target_user_id, status='approved'
        ).exists()
    return False


def accessible_user_ids(requester):
    """
    User ids whose training data `requester` may read: themselves, plus approved
    clients if they are a trainer, or everyone if admin (returns None = unrestricted).
    """
    if getattr(requester, 'is_admin', False):
        return None  # unrestricted
    ids = {requester.id}
    if getattr(requester, 'is_trainer', False):
        # The approved relation, which is the platform's single source of truth for
        # "this trainer may see this client" — the same record `can_access_user_data`
        # reads. Nothing here consults the `assigned_trainer` pointer.
        from users.models import TrainerClientRelation
        ids.update(
            TrainerClientRelation.objects.filter(
                trainer=requester, status='approved'
            ).values_list('client_id', flat=True)
        )
    return ids


# ============================================================================
# Shared exercise visibility
# ============================================================================
def visible_exercises_for(requester, base_qs=None):
    """
    Single source of truth for "which exercises may `requester` READ?".

    Visible to:
      * admin — every exercise,
      * a trainer — their own (`created_by=requester`) plus the shared catalogue
        (`created_by IS NULL`, the rows `is_global` follows),
      * a client — the shared catalogue, anything created by a trainer they have an
        APPROVED relation with, and anything appearing in a routine assigned to them.

    Everything else is another trainer's private exercise: not listed, not readable,
    and answered exactly like an id that does not exist.

    This rule lived only inside `ExerciseViewSet.get_queryset`, so every endpoint that
    resolved an exercise by id for itself simply did not have it:
    `ExerciseAddMediaView.get` served any exercise's media — photos, video URLs, form
    tips — to any signed-in caller for any id. Any endpoint that takes an exercise id
    resolves it through here, and returns the not-found answer when it misses.

    `base_qs` lets a caller keep its own `select_related`/`prefetch_related`; the
    filtering is identical either way.
    """
    from .models import Exercise

    qs = Exercise.objects.all() if base_qs is None else base_qs

    if requester is None or not getattr(requester, 'is_authenticated', False):
        return qs.none()
    if getattr(requester, 'is_admin', False):
        return qs
    if getattr(requester, 'is_trainer', False):
        return qs.filter(Q(created_by=requester) | Q(created_by__isnull=True))

    # Clients. `created_by__in=<approved relations>`, never
    # `created_by=user.assigned_trainer` — that column is the display pointer, and when
    # the two disagreed it was the pointer that granted access without consent.
    from users.models import TrainerClientRelation
    trainers = (TrainerClientRelation.objects
                .filter(client=requester, status='approved')
                .values_list('trainer_id', flat=True))
    return qs.filter(
        Q(created_by__isnull=True)
        | Q(created_by_id__in=trainers)
        | Q(routine_exercises__routine__assigned_to=requester)
    ).distinct()
