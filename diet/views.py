from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from .tasks import generate_ai_diet_plan
from .models import DailyAdvice
import json

class GenerateDietPlanView(APIView):
    def post(self, request):
        user = request.user
        meal_count = request.data.get('meal_count', 3)
        
        # Validate meal count
        if meal_count not in [3,4,5]:
            return Response(
                {"error": "Invalid meal count. Choose 3,4 or 5"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Trigger async generation
        generate_ai_diet_plan.delay(user.id, meal_count)
        return Response(
            {"status": "Generation started. Check back in 1-2 minutes."},
            status=status.HTTP_202_ACCEPTED
        )

class DailyAdviceView(APIView):
    def get(self, request):
        advice = DailyAdvice.objects.filter(
            user=request.user
        ).order_by('-generated_at').first()
        
        if not advice:
            return Response(
                {"error": "No advice generated yet"},
                status=status.HTTP_404_NOT_FOUND
            )
            
        return Response({
            "text": advice.text,
            "generated_at": advice.generated_at
        })