#!/usr/bin/env python3
"""
Check and fix user authentication
"""

import os
import django

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

from users.models import CustomUser
from django.contrib.auth.hashers import make_password

def check_and_fix_user():
    """Check user status and fix if needed"""
    
    email = "ll@gmail.com"
    password = "testpass123"
    
    print(f"🔍 Checking user: {email}")
    
    # Check if user exists
    user = CustomUser.objects.filter(email=email).first()
    
    if not user:
        print(f"❌ User {email} does not exist!")
        print("Creating new trainer user...")
        
        user = CustomUser.objects.create_user(
            email=email,
            password=password,
            user_type='trainer',
            first_name='Test',
            last_name='Trainer',
            is_active=True
        )
        print(f"✅ Created new trainer user: {email}")
    else:
        print(f"✅ User exists: {email}")
        print(f"   ID: {user.id}")
        print(f"   Type: {user.user_type}")
        print(f"   Active: {user.is_active}")
        print(f"   Password set: {user.password.startswith('pbkdf2') if user.password else 'No password'}")
        
        # Fix password if needed
        if not user.password or not user.password.startswith('pbkdf2'):
            print("🔧 Setting password...")
            user.password = make_password(password)
            user.save()
            print("✅ Password updated")
        
        # Ensure user is active
        if not user.is_active:
            print("🔧 Activating user...")
            user.is_active = True
            user.save()
            print("✅ User activated")
    
    # Test authentication
    print("\n🧪 Testing authentication...")
    try:
        from django.contrib.auth import authenticate
        authenticated_user = authenticate(email=email, password=password)
        
        if authenticated_user:
            print("✅ Authentication successful!")
            print(f"   User: {authenticated_user.email}")
            print(f"   Type: {authenticated_user.user_type}")
        else:
            print("❌ Authentication failed!")
            
    except Exception as e:
        print(f"❌ Authentication error: {e}")
    
    print("\n📋 User Summary:")
    print(f"   Email: {email}")
    print(f"   Password: {password}")
    print(f"   Type: trainer")
    print(f"   Active: True")

if __name__ == "__main__":
    check_and_fix_user() 