#!/usr/bin/env python3
import os
import django

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

from users.models import CustomUser

def reset_test_passwords():
    """Reset test user passwords"""
    print("🔧 RESETTING TEST USER PASSWORDS")
    print("=" * 40)
    
    # Reset admin password
    try:
        admin = CustomUser.objects.get(email="admin@test.com")
        admin.set_password('testpass123')
        admin.save()
        print(f"✅ Reset password for admin: {admin.username}")
    except CustomUser.DoesNotExist:
        print("❌ Admin not found")
    
    # Reset trainer1 password
    try:
        trainer1 = CustomUser.objects.get(email="trainer1@test.com")
        trainer1.set_password('testpass123')
        trainer1.save()
        print(f"✅ Reset password for trainer1: {trainer1.username}")
    except CustomUser.DoesNotExist:
        print("❌ Trainer1 not found")
    
    # Reset client password
    try:
        client = CustomUser.objects.get(email="client@test.com")
        client.set_password('testpass123')
        client.save()
        print(f"✅ Reset password for client: {client.username}")
    except CustomUser.DoesNotExist:
        print("❌ Client not found")
    
    print("\n✅ All test user passwords reset to 'testpass123'")

if __name__ == "__main__":
    reset_test_passwords() 