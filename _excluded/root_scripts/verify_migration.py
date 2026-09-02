
import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

from users.models import CustomUser
from diet.models import DietPlan
from routine.models import Routine

def verify():
    user_count = CustomUser.objects.count()
    diet_plan_count = DietPlan.objects.count()
    routine_count = Routine.objects.count()
    
    print(f"Users: {user_count}")
    print(f"Diet Plans: {diet_plan_count}")
    print(f"Routines: {routine_count}")
    
    # Simple check
    if user_count > 0:
        print("VERIFICATION SUCCESS: Data matches expectation (at least some users exist).")
    else:
        print("VERIFICATION FAILURE: No users found.")

if __name__ == '__main__':
    verify()
