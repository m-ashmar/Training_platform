from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import (
    Routine, Exercise, RoutineExercise, RoutineProgress, ExerciseSetLog
)
from .serializers import (
    RoutineSerializer, ExerciseSerializer, RoutineExerciseSerializer,
    RoutineProgressSerializer, ExerciseSetLogSerializer
)
from .permissions import IsAdminOrOwnerOrReadOnly


class ExerciseViewSet(viewsets.ModelViewSet):
    """ViewSet for managing exercises."""
    queryset = Exercise.objects.all()
    serializer_class = ExerciseSerializer
    permission_classes = [IsAdminOrOwnerOrReadOnly]


class RoutineViewSet(viewsets.ModelViewSet):
    """ViewSet for managing routines."""
    queryset = Routine.objects.all()
    serializer_class = RoutineSerializer
    permission_classes = [IsAdminOrOwnerOrReadOnly]

    def get_queryset(self):
        # Return only the routines that the user is associated with
        return self.queryset.filter(assigned_to=self.request.user)

    def perform_create(self, serializer):
        """Override to assign the routine to the creator."""
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=['post'])
    def update_progress(self, request, pk=None):
        """Custom action to update progress for a user on a routine."""
        routine = self.get_object()
        user = request.user
        day = request.data.get("day")
        status = request.data.get("status")

        if user not in routine.assigned_to.all():
            return Response({"error": "You are not authorized to update this routine."}, status=403)

        if not day or not status:
            return Response({"error": "Day and status are required."}, status=400)

        # Validate if the day exists in the routine's exercises
        valid_days = [exercise.day for exercise in routine.routine_exercises.all()]
        if int(day) not in valid_days:
            return Response({"error": f"Day {day} is not part of the routine."}, status=400)

        try:
            updated_progress = routine.update_progress(user=user, day=int(day), status=status)
        except ValueError as e:
            return Response({"error": str(e)}, status=400)

        return Response(
            {"message": "Progress updated successfully.", "updated_progress": updated_progress},
            status=200
        )


class RoutineExerciseViewSet(viewsets.ModelViewSet):
    """ViewSet for managing exercises within routines."""
    queryset = RoutineExercise.objects.all()
    serializer_class = RoutineExerciseSerializer
    permission_classes = [IsAdminOrOwnerOrReadOnly]


class RoutineProgressViewSet(viewsets.ModelViewSet):
    """ViewSet for tracking user progress on routines."""
    queryset = RoutineProgress.objects.all()
    serializer_class = RoutineProgressSerializer
    permission_classes = [IsAdminOrOwnerOrReadOnly]

    def get_queryset(self):
        # Filter progress for the logged-in user
        return self.queryset.filter(user=self.request.user)


class ExerciseSetLogViewSet(viewsets.ModelViewSet):
    """ViewSet for managing exercise set logs."""
    queryset = ExerciseSetLog.objects.all()
    serializer_class = ExerciseSetLogSerializer
    permission_classes = [IsAdminOrOwnerOrReadOnly]

    def perform_create(self, serializer):
        """Assign the logged-in user to the set log."""
        serializer.save(user=self.request.user)