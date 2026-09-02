#!/usr/bin/env python3
import os
import django

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

from users.models import CustomUser
from routine.models import RoutineTemplate

def debug_template_visibility():
    """Debug the template visibility logic"""
    print("🔍 DEBUGGING TEMPLATE VISIBILITY")
    print("=" * 50)
    
    # Get test users
    admin = CustomUser.objects.get(email="admin@test.com")
    trainer1 = CustomUser.objects.get(email="trainer1@test.com")
    trainer2 = CustomUser.objects.get(email="trainer2@test.com")
    client = CustomUser.objects.get(email="client@test.com")
    
    print(f"Admin: {admin.username} (ID: {admin.id})")
    print(f"Trainer1: {trainer1.username} (ID: {trainer1.id})")
    print(f"Trainer2: {trainer2.username} (ID: {trainer2.id})")
    print(f"Client: {client.username} (ID: {client.id})")
    
    print("\n📊 TEMPLATE BREAKDOWN:")
    print("-" * 30)
    
    # Get all templates
    all_templates = RoutineTemplate.objects.all()
    print(f"Total templates in system: {all_templates.count()}")
    
    # Breakdown by creator
    for template in all_templates:
        print(f"  - {template.name} (ID: {template.id})")
        print(f"    Created by: {template.created_by.username} (ID: {template.created_by.id})")
        print(f"    Public: {template.is_public}")
        print(f"    Goal: {template.goal}")
        print()
    
    print("\n👁️ VISIBILITY ANALYSIS:")
    print("-" * 30)
    
    # Test admin visibility
    admin_templates = RoutineTemplate.objects.all()
    print(f"Admin can see: {admin_templates.count()} templates")
    
    # Test trainer1 visibility
    trainer1_templates = RoutineTemplate.objects.filter(
        models.Q(created_by=trainer1) | models.Q(is_public=True)
    ).distinct()
    trainer1_own = trainer1_templates.filter(created_by=trainer1)
    trainer1_public = trainer1_templates.filter(is_public=True)
    
    print(f"Trainer1 can see: {trainer1_templates.count()} templates")
    print(f"  - Own templates: {trainer1_own.count()}")
    print(f"  - Public templates: {trainer1_public.count()}")
    
    # Test trainer2 visibility
    trainer2_templates = RoutineTemplate.objects.filter(
        models.Q(created_by=trainer2) | models.Q(is_public=True)
    ).distinct()
    trainer2_own = trainer2_templates.filter(created_by=trainer2)
    trainer2_public = trainer2_templates.filter(is_public=True)
    
    print(f"Trainer2 can see: {trainer2_templates.count()} templates")
    print(f"  - Own templates: {trainer2_own.count()}")
    print(f"  - Public templates: {trainer2_public.count()}")
    
    # Test client visibility
    client_templates = RoutineTemplate.objects.filter(is_public=True)
    print(f"Client can see: {client_templates.count()} templates (public only)")
    
    print("\n🔍 DETAILED TRAINER1 TEMPLATES:")
    print("-" * 30)
    for template in trainer1_templates:
        is_own = template.created_by == trainer1
        print(f"  - {template.name} (Own: {is_own}, Public: {template.is_public})")
    
    print("\n🔍 DETAILED TRAINER2 TEMPLATES:")
    print("-" * 30)
    for template in trainer2_templates:
        is_own = template.created_by == trainer2
        print(f"  - {template.name} (Own: {is_own}, Public: {template.is_public})")

if __name__ == "__main__":
    from django.db import models
    debug_template_visibility() 