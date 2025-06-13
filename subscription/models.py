from django.db import models
from users.models import CustomUser
from datetime import timedelta

class Subscription(models.Model):
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE)
    start_date = models.DateTimeField(auto_now_add=True)
    end_date = models.DateTimeField()
    active = models.BooleanField(default=True)
    has_diet_access = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        if not self.end_date:
            self.end_date = self.start_date + timedelta(days=30)  # Default 1-month subscription
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user.username} - Active: {self.active}"