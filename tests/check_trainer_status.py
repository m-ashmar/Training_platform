#!/usr/bin/env python3
import os
import django

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

from users.models import CustomUser

def check_trainer_status():
    """Check the status of the specific trainer"""
    print("🔍 CHECKING TRAINER STATUS")
    print("=" * 50)
    
    # Find trainer with email as@gmail.com
    try:
        trainer = CustomUser.objects.get(email="as@gmail.com")
        print(f"✅ Found trainer: {trainer.username} ({trainer.email})")
        print(f"   User ID: {trainer.id}")
        print(f"   User Type: {trainer.user_type}")
        print(f"   Is Active: {trainer.is_active}")
        print(f"   Is Available: {trainer.trainer_is_available}")
        print(f"   Is Verified: {trainer.trainer_is_verified}")
        print(f"   Full Name: {trainer.full_name}")
        
        # Check if trainer is available
        if not trainer.trainer_is_available:
            print("⚠️  Trainer is not available for new clients")
            print("   Setting trainer_is_available to True...")
            trainer.trainer_is_available = True
            trainer.save()
            print("✅ Trainer is now available")
        else:
            print("✅ Trainer is available for new clients")
        
        # Check if trainer is active
        if not trainer.is_active:
            print("⚠️  Trainer account is not active")
            print("   Setting is_active to True...")
            trainer.is_active = True
            trainer.save()
            print("✅ Trainer account is now active")
        else:
            print("✅ Trainer account is active")
            
        return True
        
    except CustomUser.DoesNotExist:
        print("❌ Trainer with email 'as@gmail.com' not found")
        return False

def check_available_trainers():
    """Check all available trainers"""
    print("\n🔍 CHECKING ALL AVAILABLE TRAINERS")
    print("=" * 50)
    
    available_trainers = CustomUser.objects.filter(
        user_type='trainer',
        is_active=True,
        trainer_is_available=True
    )
    
    print(f"Found {available_trainers.count()} available trainers:")
    
    for trainer in available_trainers[:10]:  # Show first 10
        print(f"  - {trainer.username} ({trainer.email}) - ID: {trainer.id}")
    
    if available_trainers.count() > 10:
        print(f"  ... and {available_trainers.count() - 10} more")

if __name__ == "__main__":
    check_trainer_status()
    check_available_trainers() 