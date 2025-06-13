from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ExerciseViewSet, RoutineViewSet, RoutineProgressViewSet, ExerciseSetLogViewSet

# Create a router and register viewsets
router = DefaultRouter()
router.register(r'exercises', ExerciseViewSet, basename='exercise')
router.register(r'routines', RoutineViewSet, basename='routine')
router.register(r'routine-progress', RoutineProgressViewSet, basename='routine_progress')  # Fixed quote issue
router.register(r'set-logs', ExerciseSetLogViewSet, basename='set_logs')

# Register router URLs
urlpatterns = [
    path('', include(router.urls)),  # Registers all ViewSets
]