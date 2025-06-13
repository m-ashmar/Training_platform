from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone
import logging
from datetime import date , timedelta






logger = logging.getLogger(__name__)
class CustomUserManager(BaseUserManager):
    def create_user(self, email, username, password=None, phone_number=None, **extra_fields):
     
        if not email:
            raise ValueError("The Email field must be set")
        extra_fields.setdefault('is_active', True)     
        email = self.normalize_email(email)
        user = self.model(email=email, username=username,phone_number=phone_number, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, username, password=None, phone_number=None, **extra_fields):
     
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)

        return self.create_user(email, username, password, **extra_fields)

class CustomUser(AbstractBaseUser, PermissionsMixin):
    username = models.CharField(max_length=150, unique=True)
    email = models.EmailField(unique=True)
    date_joined = models.DateTimeField( default=timezone.now)
    phone_number = models.CharField(max_length=15, default="0000000000", null=False, blank=True)   
    first_name = models.CharField(max_length=30, blank=True)  # Add this
    last_name = models.CharField(max_length=30, blank=True)   # Add this
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    height = models.FloatField(null=True, blank=True)  # in cm
    weight = models.FloatField(null=True, blank=True)  # in kg
    age = models.PositiveIntegerField(null=True, blank=True)
    gender = models.CharField(max_length=10, choices=[('Male', 'Male'), ('Female', 'Female')], null=True, blank=True)
    specific_injury = models.TextField(null=True, blank=True)

    objects = CustomUserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username', 'phone_number'] # Make sure phone_number is in REQUIRED_FIELDS
    activity_level = models.CharField( 
    max_length=20,
    choices=[
        ('Sedentary', 'Sedentary'),
        ('Light', 'Light Exercise'),
        ('Moderate', 'Moderate Exercise'),
        ('Active', 'Active'),
        ('VeryActive', 'Very Active')
    ],
    default='Sedentary'
)
    
    def calculate_bmi(self):
        if self.height and self.weight:
            return round(self.weight / ((self.height/100) ** 2), 2)
        return None


    def calculate_bmr(self):
        if self.gender and self.height and self.weight and self.age:
            if self.gender == 'Male':
                return 10*self.weight + 6.25*self.height - 5*self.age + 5
            return 10*self.weight + 6.25*self.height - 5*self.age - 161
        return None
    
        


    def calculate_daily_calories(self , goal='Maintain'): 
        required_fields = [self.height, self.weight, self.age, self.gender]
        if any(field is None for field in required_fields):
            raise ValueError("Complete profile required: height, weight, age, gender")# FIXED METHOD SIGNATURE
        bmr = self.calculate_bmr()
        if bmr is None:
            messages.warning(request, f"BMR data incomplete for user: {preference.user}")
            return None
            
        activity_factors = {
            'Sedentary': 1.2,
            'Light': 1.375,
            'Moderate': 1.55,
            'Active': 1.725,
            'VeryActive': 1.9
        }
        maintenance = bmr * activity_factors.get(self.activity_level, 1.2)
        
        try:
            if self.dietplan.goal == 'Lose':
                return maintenance - 500
            elif self.dietplan.goal == 'Gain':
                return maintenance + 500
        except AttributeError:
            pass
        return maintenance

    def generate_diet_plan(self, goal='Maintain' , duration_weeks=4):
        
        from diet.models import DietPlan
        from diet.services import DietOptimizer
        from datetime import date , timedelta

        DietPlan.objects.create(
            user=self,
            goal=goal,
            daily_calories=self.calculate_daily_calories(goal),
            start_date=date.today(),
            end_date=date.today() + timedelta(weeks=duration_weeks),
            duration_weeks=duration_weeks
    )
    
        optimizer = DietOptimizer(self)
        return optimizer.optimize()