from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    ExerciseViewSet, ExerciseCreateWithImageView, RoutineViewSet, 
    RoutineProgressViewSet, ExerciseSetLogViewSet, 
    RoutineExerciseViewSet, WorkoutSessionViewSet, AnalyticsViewSet, 
    RoutineTemplateViewSet, UserExerciseProgressViewSet, 
    ExerciseImageUploadView, ExerciseAddMediaView, TrainerClientProgressViewSet
)

app_name = 'routine'

# Create a router and register viewsets
router = DefaultRouter()
router.register(r'exercises', ExerciseViewSet, basename='exercise')
router.register(r'routines', RoutineViewSet, basename='routine')
router.register(r'routine-progress', RoutineProgressViewSet, basename='routine_progress')  # Fixed quote issue
router.register(r'set-logs', ExerciseSetLogViewSet, basename='set_logs')
router.register(r'exercisesetlogs', ExerciseSetLogViewSet, basename='exercisesetlog')  # Added for test compatibility
router.register(r'routineexercises', RoutineExerciseViewSet, basename='routineexercise')  # Added for full test coverage
router.register(r'workoutsessions', WorkoutSessionViewSet, basename='workoutsession')  # Added for full test coverage
router.register(r'analytics', AnalyticsViewSet, basename='analytics')
router.register(r'templates', RoutineTemplateViewSet, basename='templatetemplate')
router.register(r'user-exercise-progress', UserExerciseProgressViewSet, basename='userexerciseprogress')

# Register router URLs
urlpatterns = [
    # Explicit custom actions for routine assignment/unassignment and exercise image endpoints
    path('exercises/create-with-image/', ExerciseCreateWithImageView.as_view(), name='exercise-create-with-image'),
    path('exercises/<int:exercise_id>/image/', ExerciseImageUploadView.as_view(), name='exercise-image-upload'),
    path('exercises/<int:exercise_id>/add-media/', ExerciseAddMediaView.as_view(), name='exercise-add-media'),
    path('routines/<int:pk>/assign_to_client/', RoutineViewSet.as_view({'post': 'assign_to_client'}), name='routine-assign-to-client'),
    path('routines/<int:pk>/unassign_from_client/', RoutineViewSet.as_view({'post': 'unassign_from_client'}), name='routine-unassign-from-client'),
    # Trainer client progress endpoints
    path('trainer/client-progress/<int:client_id>/', TrainerClientProgressViewSet.as_view({'get': 'client_progress'}), name='trainer-client-progress'),
    path('trainer/client-progress/recent/', TrainerClientProgressViewSet.as_view({'get': 'recent_progress'}), name='trainer-client-recent-progress'),
    path('', include(router.urls)),  # Registers all ViewSets
]