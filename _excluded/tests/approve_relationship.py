#!/usr/bin/env python3
import os
import django

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

from users.models import TrainerClientRelation, CustomUser

# Approve the relationship for the latest test run
trainer = CustomUser.objects.get(id=112)  # Latest trainer from test
client = CustomUser.objects.get(id=113)   # Latest client from test

try:
    relation = TrainerClientRelation.objects.get(trainer=trainer, client=client)
    relation.status = 'approved'
    relation.save()
    print(f"✅ Relationship approved successfully!")
    print(f"   Trainer: {trainer.username} (ID: {trainer.id})")
    print(f"   Client: {client.username} (ID: {client.id})")
    print(f"   Status: {relation.status}")
except TrainerClientRelation.DoesNotExist:
    print("❌ No relationship found between trainer and client")
except Exception as e:
    print(f"❌ Error: {str(e)}") 