from django.urls import path
from .views import GenerateDietPlanView, DailyAdviceView

urlpatterns = [
    path('generate-plan/', GenerateDietPlanView.as_view(), name='generate-diet-plan'),
    path('daily-advice/', DailyAdviceView.as_view(), name='daily-advice'),
]