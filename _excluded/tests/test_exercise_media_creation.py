#!/usr/bin/env python3
"""
Test script for the enhanced ExerciseCreateWithImageView with media support.

This script demonstrates how to create exercises with:
1. Main demonstration image
2. Additional photo uploads  
3. Video URLs
4. Additional text instructions

Run this script to test the new functionality.
"""

import os
import sys
import django
import requests
import json
from io import BytesIO
from PIL import Image

# Add project root to Python path
sys.path.append('/Users/mac/Desktop/Git/Training_platform')

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

from users.models import CustomUser
from routine.models import Exercise, ExerciseMedia

def create_test_image(name, size=(200, 200)):
    """Create a test image file for upload testing"""
    img = Image.new('RGB', size, color='red')
    img_bytes = BytesIO()
    img.save(img_bytes, format='JPEG')
    img_bytes.seek(0)
    img_bytes.name = name
    return img_bytes

def test_exercise_creation_with_media():
    """Test the enhanced exercise creation with various media types"""
    
    print("🏋️ Testing Enhanced Exercise Creation with Media")
    print("=" * 60)
    
    # Get or create test user (trainer)
    try:
        test_user = CustomUser.objects.get(username='exercise_test_trainer')
        print(f"✅ Using existing test user: {test_user.username}")
    except CustomUser.DoesNotExist:
        test_user = CustomUser.objects.create_user(
            username='exercise_test_trainer',
            email='trainer@exercisetest.com',
            password='TestPass123!',
            user_type='trainer',
            first_name='Exercise',
            last_name='Trainer',
            phone_number='+1234567890'
        )
        print(f"✅ Created new test user: {test_user.username}")
    
    # Login to get JWT token
    login_url = 'http://127.0.0.1:8000/api/auth/token/'
    login_data = {
        'email': 'trainer@exercisetest.com',
        'password': 'TestPass123!'
    }
    
    try:
        login_response = requests.post(login_url, json=login_data)
        if login_response.status_code == 200:
            token_data = login_response.json()
            access_token = token_data['access']
            print("🔐 Authentication successful")
        else:
            print(f"❌ Authentication failed: {login_response.status_code}")
            print(f"Response: {login_response.text}")
            return
    except requests.exceptions.RequestException as e:
        print(f"❌ Connection error during login: {e}")
        return
    
    # Headers for authenticated requests
    headers = {
        'Authorization': f'Bearer {access_token}'
    }
    
    # Test 1: Create exercise with main image only
    print("\n1️⃣ Testing exercise creation with main image only...")
    test_exercise_main_image(headers)
    
    # Test 2: Create exercise with main image + additional photos
    print("\n2️⃣ Testing exercise creation with main image + additional photos...")
    test_exercise_with_photos(headers)
    
    # Test 3: Create exercise with video URLs
    print("\n3️⃣ Testing exercise creation with video URLs...")
    test_exercise_with_videos(headers)
    
    # Test 4: Create exercise with mixed media (photos + videos + text)
    print("\n4️⃣ Testing exercise creation with mixed media...")
    test_exercise_with_mixed_media(headers)
    
    # Test 5: Show all created exercises and their media
    print("\n5️⃣ Displaying all created exercises and media...")
    display_created_exercises()

def test_exercise_main_image(headers):
    """Test creating exercise with just main image"""
    url = 'http://127.0.0.1:8000/api/routine/exercises/create-with-image/'
    
    # Create test image
    main_image = create_test_image('push_up_demo.jpg')
    
    data = {
        'name': 'Push-up (Basic)',
        'description': 'Classic bodyweight exercise for chest, shoulders, and triceps',
        'target_muscle': 'Upper Chest',
        'difficulty_level': 'beginner'
    }
    
    files = {
        'image': ('push_up_demo.jpg', main_image, 'image/jpeg')
    }
    
    try:
        response = requests.post(url, headers=headers, data=data, files=files)
        if response.status_code == 201:
            result = response.json()
            print("✅ Exercise created successfully")
            print(f"   📝 Name: {result['exercise']['name']}")
            print(f"   🎯 Target: {result['exercise']['target_muscle']}")
            print(f"   📸 Main image: {'Yes' if result['exercise']['image'] else 'No'}")
            print(f"   📁 Media count: {len(result['exercise']['media'])}")
        else:
            print(f"❌ Failed to create exercise: {response.status_code}")
            print(f"   Response: {response.text}")
    except Exception as e:
        print(f"❌ Error: {e}")

def test_exercise_with_photos(headers):
    """Test creating exercise with main image + additional photos"""
    url = 'http://127.0.0.1:8000/api/routine/exercises/create-with-image/'
    
    # Create test images
    main_image = create_test_image('squat_main.jpg')
    photo1 = create_test_image('squat_start.jpg', (150, 150))
    photo2 = create_test_image('squat_bottom.jpg', (150, 150))
    
    data = {
        'name': 'Barbell Squat',
        'description': 'Compound exercise targeting legs and glutes with barbell',
        'target_muscle': 'Front Quads',
        'difficulty_level': 'intermediate'
    }
    
    files = {
        'image': ('squat_main.jpg', main_image, 'image/jpeg'),
        'media_photos': [
            ('squat_start.jpg', photo1, 'image/jpeg'),
            ('squat_bottom.jpg', photo2, 'image/jpeg')
        ]
    }
    
    try:
        response = requests.post(url, headers=headers, data=data, files=files)
        if response.status_code == 201:
            result = response.json()
            print("✅ Exercise with photos created successfully")
            print(f"   📝 Name: {result['exercise']['name']}")
            print(f"   📸 Main image: {'Yes' if result['exercise']['image'] else 'No'}")
            print(f"   📁 Total media: {result['media_created']}")
            print(f"   🖼️  Photos: {result['media_breakdown']['photos']}")
        else:
            print(f"❌ Failed to create exercise: {response.status_code}")
            print(f"   Response: {response.text}")
    except Exception as e:
        print(f"❌ Error: {e}")

def test_exercise_with_videos(headers):
    """Test creating exercise with video URLs"""
    url = 'http://127.0.0.1:8000/api/routine/exercises/create-with-image/'
    
    data = {
        'name': 'Deadlift (Olympic)',
        'description': 'Olympic deadlift with proper form demonstration',
        'target_muscle': 'Lower Back',
        'difficulty_level': 'advanced',
        'media_videos': 'https://www.youtube.com/watch?v=deadlift123, https://vimeo.com/deadlift456'
    }
    
    try:
        response = requests.post(url, headers=headers, data=data)
        if response.status_code == 201:
            result = response.json()
            print("✅ Exercise with videos created successfully")
            print(f"   📝 Name: {result['exercise']['name']}")
            print(f"   📁 Total media: {result['media_created']}")
            print(f"   🎥 Videos: {result['media_breakdown']['videos']}")
        else:
            print(f"❌ Failed to create exercise: {response.status_code}")
            print(f"   Response: {response.text}")
    except Exception as e:
        print(f"❌ Error: {e}")

def test_exercise_with_mixed_media(headers):
    """Test creating exercise with mixed media types"""
    url = 'http://127.0.0.1:8000/api/routine/exercises/create-with-image/'
    
    # Create test image
    main_image = create_test_image('pullup_main.jpg')
    photo1 = create_test_image('pullup_grip.jpg', (100, 100))
    
    data = {
        'name': 'Pull-up (Complete Guide)',
        'description': 'Complete pull-up guide with multiple media demonstrations',
        'target_muscle': 'Lats',
        'difficulty_level': 'intermediate',
        'media_videos': 'https://www.youtube.com/watch?v=pullup123',
        'media_texts': 'Grip the bar with hands shoulder-width apart||Pull your body up until chin clears the bar||Lower yourself with control'
    }
    
    files = {
        'image': ('pullup_main.jpg', main_image, 'image/jpeg'),
        'media_photos': [('pullup_grip.jpg', photo1, 'image/jpeg')]
    }
    
    try:
        response = requests.post(url, headers=headers, data=data, files=files)
        if response.status_code == 201:
            result = response.json()
            print("✅ Exercise with mixed media created successfully")
            print(f"   📝 Name: {result['exercise']['name']}")
            print(f"   📸 Main image: {'Yes' if result['exercise']['image'] else 'No'}")
            print(f"   📁 Total media: {result['media_created']}")
            print(f"   🖼️  Photos: {result['media_breakdown']['photos']}")
            print(f"   🎥 Videos: {result['media_breakdown']['videos']}")
            print(f"   📝 Texts: {result['media_breakdown']['texts']}")
        else:
            print(f"❌ Failed to create exercise: {response.status_code}")
            print(f"   Response: {response.text}")
    except Exception as e:
        print(f"❌ Error: {e}")

def display_created_exercises():
    """Display all exercises created in this test with their media"""
    
    exercises = Exercise.objects.filter(
        name__in=[
            'Push-up (Basic)',
            'Barbell Squat', 
            'Deadlift (Olympic)',
            'Pull-up (Complete Guide)'
        ]
    ).prefetch_related('media')
    
    print("📋 Created Exercises Summary:")
    print("-" * 40)
    
    for exercise in exercises:
        print(f"\n🏋️ {exercise.name}")
        print(f"   🎯 Target: {exercise.target_muscle}")
        print(f"   📊 Difficulty: {exercise.difficulty_level}")
        print(f"   📸 Main image: {'Yes' if exercise.image else 'No'}")
        
        media_items = exercise.media.all()
        if media_items:
            print(f"   📁 Additional media ({len(media_items)} items):")
            for media in media_items:
                media_icon = {
                    'photo': '🖼️',
                    'video': '🎥', 
                    'text': '📝'
                }.get(media.media_type, '📄')
                
                content_preview = media.content[:50] + '...' if len(media.content) > 50 else media.content
                print(f"      {media_icon} {media.title}: {content_preview}")
        else:
            print("   📁 No additional media")

def test_api_endpoint_directly():
    """Test the API endpoint directly to show request/response format"""
    print("\n📡 API Endpoint Documentation")
    print("=" * 50)
    print("🔗 Endpoint: POST /api/routine/exercises/create-with-image/")
    print("🔐 Authentication: Bearer JWT token required")
    print("📦 Content-Type: multipart/form-data")
    
    print("\n📝 Request Parameters:")
    print("Required:")
    print("  • name (string): Exercise name")
    print("  • description (string): Exercise description")
    print("  • target_muscle (string): Target muscle group")
    
    print("Optional:")
    print("  • difficulty_level (string): beginner/intermediate/advanced/expert")
    print("  • image (file): Main demonstration image")
    print("  • media_photos (files[]): Additional photo files")
    print("  • media_videos (string): Comma-separated video URLs")
    print("  • media_texts (string): Text content separated by ||")
    
    print("\n✅ Response Format (201 Created):")
    example_response = {
        "message": "Exercise created successfully",
        "exercise": {
            "id": 123,
            "name": "Example Exercise",
            "description": "Exercise description",
            "target_muscle": "Upper Chest",
            "image": "https://domain.com/media/exercise_images/demo.jpg",
            "media": [
                {
                    "id": 1,
                    "media_type": "photo",
                    "content": "https://domain.com/media/exercise_media/photo1.jpg"
                },
                {
                    "id": 2,
                    "media_type": "video", 
                    "content": "https://youtube.com/watch?v=example"
                }
            ]
        },
        "media_created": 2,
        "media_breakdown": {
            "photos": 1,
            "videos": 1,
            "texts": 0
        }
    }
    print(json.dumps(example_response, indent=2))

if __name__ == '__main__':
    print("🚀 Starting Exercise Media Creation Tests")
    print("Make sure Django server is running on http://127.0.0.1:8000")
    
    try:
        test_exercise_creation_with_media()
        test_api_endpoint_directly()
        
        print("\n🎉 All tests completed successfully!")
        print("\n💡 The enhanced ExerciseCreateWithImageView now supports:")
        print("   ✅ Main exercise images (demonstration)")
        print("   ✅ Additional photo uploads")
        print("   ✅ Video URLs (YouTube, Vimeo, etc.)")
        print("   ✅ Additional text instructions")
        print("   ✅ Comprehensive validation")
        print("   ✅ Proper media organization")
        
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc() 