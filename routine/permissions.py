from rest_framework.permissions import BasePermission, SAFE_METHODS


class IsAdminOrOwnerOrReadOnly(BasePermission):
    """
    Custom permission to allow:
    - Only admin users (is_staff) or trainers to create, update, or delete objects.
    - Trainers can only modify their own objects.
    - All users have read-only access (GET, HEAD, OPTIONS).
    """

    def has_permission(self, request, view):
        # Allow safe methods (GET, HEAD, OPTIONS) for all users
        if request.method in SAFE_METHODS:
            return True
        # Only trainers and admins can create, update, or delete objects
        return request.user and (request.user.is_trainer or request.user.is_staff)

    def has_object_permission(self, request, view, obj):
        # Allow safe methods for all users
        if request.method in SAFE_METHODS:
            return True
        # Allow modifying methods for admins
        if request.user and request.user.is_staff:
            return True
        # Allow modifying methods for trainers if they own the object
        if request.user and request.user.is_trainer and hasattr(obj, 'created_by'):
            return obj.created_by == request.user
        return False


class IsTrainerOrAdmin(BasePermission):
    """
    Permission to allow only trainers and admins to access.
    """
    
    def has_permission(self, request, view):
        return request.user and (request.user.is_trainer or request.user.is_admin)


class IsClientOrTrainerOrAdmin(BasePermission):
    """
    Permission to allow clients, their assigned trainers, and admins to access.
    """
    
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated
    
    def has_object_permission(self, request, view, obj):
        user = request.user
        
        # Admins can access everything
        if user.is_admin:
            return True
            
        # For user objects, check if it's the user themselves or their trainer
        if hasattr(obj, 'user_type'):
            if obj.user_type == 'client':
                return (user == obj or user == obj.assigned_trainer)
            elif obj.user_type == 'trainer':
                return user == obj
            else:
                return user == obj
        
        # For other objects, check if user is assigned to them
        if hasattr(obj, 'assigned_to'):
            return user in obj.assigned_to.all()
        
        # For objects with created_by field
        if hasattr(obj, 'created_by'):
            return user == obj.created_by
        
        # Default to user being the owner
        return user == obj


class IsTrainerForClient(BasePermission):
    """
    Permission to allow trainers to access their assigned clients' data.
    """
    
    def has_permission(self, request, view):
        return request.user and (request.user.is_trainer or request.user.is_admin)
    
    def has_object_permission(self, request, view, obj):
        user = request.user
        
        # Admins can access everything
        if user.is_admin:
            return True
        
        # For client objects, check if user is their assigned trainer
        if hasattr(obj, 'user_type') and obj.user_type == 'client':
            return user == obj.assigned_trainer
        
        # For other objects, check if they belong to a client assigned to this trainer
        if hasattr(obj, 'user') and obj.user.user_type == 'client':
            return user == obj.user.assigned_trainer
        
        return False


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
                return (user == obj or user == obj.assigned_trainer)
            elif obj.user_type == 'trainer':
                return user == obj
        
        # For objects with user field
        if hasattr(obj, 'user'):
            if obj.user.user_type == 'client':
                return (user == obj.user or user == obj.user.assigned_trainer)
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
            return obj.assigned_to.filter(assigned_trainer=user).exists()
        
        return False


class IsExerciseOwnerOrGlobal(BasePermission):
    """
    Permission for exercises - allows creators and global access.
    """
    
    def has_permission(self, request, view):
        return request.user and request.user.is_authenticated
    
    def has_object_permission(self, request, view, obj):
        user = request.user
        
        # Admins can access everything
        if user.is_admin:
            return True
        
        # Creators can access their exercises
        if hasattr(obj, 'created_by') and user == obj.created_by:
            return True
        
        # Global exercises (no creator) are accessible to all
        if hasattr(obj, 'created_by') and obj.created_by is None:
            return True
        
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
        return request.user and (request.user.is_trainer or request.user.is_admin)

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
        return request.user and (request.user.is_trainer or request.user.is_admin)
    
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
    - Allow update/delete for trainers and admins only
    - Read-only for others
    """
    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return True
        if request.method == 'POST':
            return request.user and request.user.is_authenticated
        return request.user and (request.user.is_trainer or request.user.is_staff)

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True
        if request.method == 'POST':
            return request.user and request.user.is_authenticated
        if request.user and request.user.is_staff:
            return True
        if request.user and request.user.is_trainer:
            # Trainers can update/delete if they are assigned trainer of the client
            if hasattr(obj, 'user_exercise_progress') and obj.user_exercise_progress:
                return obj.user_exercise_progress.user.assigned_trainer == request.user
        return False 


class IsSessionOwnerOrTrainerOrAdmin(BasePermission):
    """
    Custom permission for WorkoutSessionViewSet:
    - Allow PATCH/PUT for the session's assigned user (client) and for trainers/admins
    - Allow POST only for trainers/admins
    - Allow GET for all authenticated users
    """
    def has_permission(self, request, view):
        if request.method in SAFE_METHODS:
            return request.user and request.user.is_authenticated
        if request.method == 'POST':
            return request.user and (request.user.is_trainer or request.user.is_staff)
        # PATCH/PUT allowed for trainers, admins, or session owner (checked in has_object_permission)
        return request.user and request.user.is_authenticated

    def has_object_permission(self, request, view, obj):
        if request.method in SAFE_METHODS:
            return True
        if request.method in ['PATCH', 'PUT']:
            # Allow if trainer, admin, or session owner
            if request.user.is_staff or (hasattr(request.user, 'is_trainer') and request.user.is_trainer):
                return True
            return obj.user == request.user
        if request.method == 'POST':
            return request.user and (request.user.is_trainer or request.user.is_staff)
        return False 