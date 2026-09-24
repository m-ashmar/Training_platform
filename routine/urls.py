from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    ExerciseViewSet, ExerciseCreateWithImageView, RoutineViewSet, 
    RoutineProgressViewSet, ExerciseSetLogViewSet, 
    RoutineExerciseViewSet, WorkoutSessionViewSet, AnalyticsViewSet, 
    RoutineTemplateViewSet, UserExerciseProgressViewSet, 
    ExerciseImageUploadView, ExerciseAddMediaView, TrainerClientProgressViewSet,
    RoutineTemplateExerciseViewSet, RecentActivityProgressView, InjuryMapView
)

app_name = 'routine'

# Create a router and register viewsets
router = DefaultRouter()
router.register(r'exercises', ExerciseViewSet, basename='exercise')
router.register(r'routines', RoutineViewSet, basename='routine')
router.register(r'routine-progress', RoutineProgressViewSet, basename='routine_progress')  # Fixed quote issue
# ONE prefix for the set-log viewset (SIDE_BUGS S-007). It was registered here twice —
# `set-logs/` and `exercisesetlogs/` — so every action it gained doubled the route
# surface and had to be written into API_CONTRACT.md twice, and the two paths drifted
# into two different documented endpoints for one viewset. The alias was added "for
# mobile app backwards compatibility" with the abandoned `mobile/` and `flutter/`
# attempts; the Flutter client in `yalla_gym/` calls neither spelling (it has no set-log
# screen yet), so nothing that ships loses a route. Removing one is not an additive
# change — see the commit message.
router.register(r'set-logs', ExerciseSetLogViewSet, basename='set_logs')
router.register(r'routine-exercises', RoutineExerciseViewSet, basename='routineexercise')  # Added for full test coverage
router.register(r'workout-sessions', WorkoutSessionViewSet, basename='workoutsession')  # Added for full test coverage
router.register(r'analytics', AnalyticsViewSet, basename='analytics')
router.register(r'templates', RoutineTemplateViewSet, basename='templatetemplate')
router.register(r'template-exercises', RoutineTemplateExerciseViewSet, basename='templateexercise')
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
    path('trainer/client-progress/attention/', TrainerClientProgressViewSet.as_view({'get': 'attention'}), name='trainer-client-attention'),
    # Injury area -> the target muscles it argues with (BACKEND_BUGS B-105)
    path('injury-map/', InjuryMapView.as_view(), name='injury-map'),
    # Recent activity progress (7-day workout duration chart)
    path('v1/analytics/recent-progress/', RecentActivityProgressView.as_view(), name='recent-activity-progress'),
    path('', include(router.urls)),  # Registers all ViewSets
]