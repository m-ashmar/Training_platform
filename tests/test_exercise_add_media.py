#!/usr/bin/env python3
"""
Test script for the new Exercise Add Media API.
Demonstrates adding videos, photos, and text to existing exercises via URLs.
"""

import requests
import json
import os
from datetime import datetime

class ExerciseAddMediaTester:
    def __init__(self, base_url="http://localhost:8000/api"):
        self.base_url = base_url
        self.token = None
        self.headers = {}
    
    def authenticate(self, email="mm@gmail.com", password="testpass123"):
        """Authenticate and get JWT token"""
        print("🔐 Authenticating...")
        
        auth_data = {
            "email": email,
            "password": password
        }
        
        try:
            response = requests.post(f"{self.base_url}/auth/token/", json=auth_data)
            if response.status_code == 200:
                data = response.json()
                self.token = data['access']
                self.headers = {
                    'Authorization': f'Bearer {self.token}',
                    'Content-Type': 'application/json'
                }
                print("✅ Authentication successful!")
                return True
            else:
                print(f"❌ Authentication failed: {response.status_code}")
                print(f"Response: {response.text}")
                return False
        except Exception as e:
            print(f"❌ Authentication error: {e}")
            return False
    
    def get_exercises(self):
        """Get available exercises"""
        print("\n📋 Getting available exercises...")
        
        try:
            response = requests.get(f"{self.base_url}/routine/exercises/", headers=self.headers)
            if response.status_code == 200:
                data = response.json()
                # Handle both list and paginated response formats
                if isinstance(data, list):
                    exercises = data
                else:
                    exercises = data.get('results', [])
                print(f"✅ Found {len(exercises)} exercises")
                return exercises
            else:
                print(f"❌ Failed to get exercises: {response.status_code}")
                return []
        except Exception as e:
            print(f"❌ Error getting exercises: {e}")
            return []
    
    def add_video_media(self, exercise_id):
        """Add video media to an exercise"""
        print(f"\n🎥 Adding video media to exercise {exercise_id}...")
        
        media_data = {
            "media_items": [
                {
                    "media_type": "video",
                    "content": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "title": "Exercise Tutorial Video",
                    "description": "Complete step-by-step tutorial for proper form",
                    "order": 1
                },
                {
                    "media_type": "video",
                    "content": "https://vimeo.com/123456789",
                    "title": "Alternative Form Video",
                    "description": "Different angle and variation demonstration",
                    "order": 2
                }
            ]
        }
        
        try:
            response = requests.post(
                f"{self.base_url}/routine/exercises/{exercise_id}/add-media/",
                json=media_data,
                headers=self.headers
            )
            
            if response.status_code in [200, 201, 207]:
                data = response.json()
                print("✅ Video media added successfully!")
                print(f"   Message: {data.get('message', 'N/A')}")
                print(f"   Created media: {len(data.get('created_media', []))}")
                print(f"   Total media count: {data.get('total_media_count', 0)}")
                
                if data.get('errors'):
                    print(f"   ⚠️  Errors: {data['errors']}")
                
                return data
            else:
                print(f"❌ Failed to add video media: {response.status_code}")
                print(f"Response: {response.text}")
                return None
        except Exception as e:
            print(f"❌ Error adding video media: {e}")
            return None
    
    def add_photo_media(self, exercise_id):
        """Add photo media to an exercise"""
        print(f"\n📸 Adding photo media to exercise {exercise_id}...")
        
        media_data = {
            "media_items": [
                {
                    "media_type": "photo",
                    "content": "https://example.com/exercise-form-front.jpg",
                    "title": "Front View Form",
                    "description": "Proper form from front angle",
                    "order": 3
                },
                {
                    "media_type": "photo",
                    "content": "https://example.com/exercise-form-side.jpg",
                    "title": "Side View Form",
                    "description": "Proper form from side angle",
                    "order": 4
                },
                {
                    "media_type": "photo",
                    "content": "https://example.com/exercise-form-back.jpg",
                    "title": "Back View Form",
                    "description": "Proper form from back angle",
                    "order": 5
                }
            ]
        }
        
        try:
            response = requests.post(
                f"{self.base_url}/routine/exercises/{exercise_id}/add-media/",
                json=media_data,
                headers=self.headers
            )
            
            if response.status_code in [200, 201, 207]:
                data = response.json()
                print("✅ Photo media added successfully!")
                print(f"   Message: {data.get('message', 'N/A')}")
                print(f"   Created media: {len(data.get('created_media', []))}")
                print(f"   Total media count: {data.get('total_media_count', 0)}")
                
                if data.get('errors'):
                    print(f"   ⚠️  Errors: {data['errors']}")
                
                return data
            else:
                print(f"❌ Failed to add photo media: {response.status_code}")
                print(f"Response: {response.text}")
                return None
        except Exception as e:
            print(f"❌ Error adding photo media: {e}")
            return None
    
    def add_text_media(self, exercise_id):
        """Add text media to an exercise"""
        print(f"\n📝 Adding text media to exercise {exercise_id}...")
        
        media_data = {
            "media_items": [
                {
                    "media_type": "text",
                    "content": "Keep your back straight and engage your core throughout the movement. Maintain proper breathing rhythm.",
                    "title": "Form Cues",
                    "description": "Important form reminders",
                    "order": 6
                },
                {
                    "media_type": "text",
                    "content": "Common mistakes: 1) Rounded back 2) Not engaging core 3) Rushing the movement 4) Improper breathing",
                    "title": "Common Mistakes",
                    "description": "Things to avoid",
                    "order": 7
                },
                {
                    "media_type": "text",
                    "content": "Progression: Start with bodyweight, then add resistance gradually. Focus on form before increasing weight.",
                    "title": "Progression Tips",
                    "description": "How to progress safely",
                    "order": 8
                }
            ]
        }
        
        try:
            response = requests.post(
                f"{self.base_url}/routine/exercises/{exercise_id}/add-media/",
                json=media_data,
                headers=self.headers
            )
            
            if response.status_code in [200, 201, 207]:
                data = response.json()
                print("✅ Text media added successfully!")
                print(f"   Message: {data.get('message', 'N/A')}")
                print(f"   Created media: {len(data.get('created_media', []))}")
                print(f"   Total media count: {data.get('total_media_count', 0)}")
                
                if data.get('errors'):
                    print(f"   ⚠️  Errors: {data['errors']}")
                
                return data
            else:
                print(f"❌ Failed to add text media: {response.status_code}")
                print(f"Response: {response.text}")
                return None
        except Exception as e:
            print(f"❌ Error adding text media: {e}")
            return None
    
    def get_exercise_media(self, exercise_id):
        """Get all media for an exercise"""
        print(f"\n📋 Getting all media for exercise {exercise_id}...")
        
        try:
            response = requests.get(
                f"{self.base_url}/routine/exercises/{exercise_id}/add-media/",
                headers=self.headers
            )
            
            if response.status_code == 200:
                data = response.json()
                print("✅ Retrieved exercise media successfully!")
                print(f"   Exercise: {data.get('exercise_name', 'N/A')}")
                print(f"   Media count: {data.get('media_count', 0)}")
                
                media_items = data.get('media_items', [])
                for i, media in enumerate(media_items, 1):
                    print(f"   {i}. {media['media_type'].upper()}: {media['title']}")
                    print(f"      Content: {media['content'][:50]}...")
                    print(f"      Order: {media['order']}")
                
                return data
            else:
                print(f"❌ Failed to get exercise media: {response.status_code}")
                print(f"Response: {response.text}")
                return None
        except Exception as e:
            print(f"❌ Error getting exercise media: {e}")
            return None
    
    def delete_media(self, exercise_id, media_ids):
        """Delete specific media items"""
        print(f"\n🗑️  Deleting media items {media_ids} from exercise {exercise_id}...")
        
        delete_data = {
            "media_ids": media_ids
        }
        
        try:
            response = requests.delete(
                f"{self.base_url}/routine/exercises/{exercise_id}/add-media/",
                json=delete_data,
                headers=self.headers
            )
            
            if response.status_code in [200, 207]:
                data = response.json()
                print("✅ Media deleted successfully!")
                print(f"   Message: {data.get('message', 'N/A')}")
                print(f"   Deleted count: {data.get('deleted_count', 0)}")
                print(f"   Remaining media: {data.get('remaining_media_count', 0)}")
                
                if data.get('errors'):
                    print(f"   ⚠️  Errors: {data['errors']}")
                
                return data
            else:
                print(f"❌ Failed to delete media: {response.status_code}")
                print(f"Response: {response.text}")
                return None
        except Exception as e:
            print(f"❌ Error deleting media: {e}")
            return None
    
    def test_mixed_media(self, exercise_id):
        """Test adding mixed media types in one request"""
        print(f"\n🎯 Testing mixed media types for exercise {exercise_id}...")
        
        media_data = {
            "media_items": [
                {
                    "media_type": "video",
                    "content": "https://www.youtube.com/watch?v=example123",
                    "title": "Mixed Media Test Video",
                    "description": "Test video for mixed media",
                    "order": 1
                },
                {
                    "media_type": "photo",
                    "content": "https://example.com/test-photo.jpg",
                    "title": "Mixed Media Test Photo",
                    "description": "Test photo for mixed media",
                    "order": 2
                },
                {
                    "media_type": "text",
                    "content": "This is a test text content for mixed media functionality.",
                    "title": "Mixed Media Test Text",
                    "description": "Test text for mixed media",
                    "order": 3
                }
            ]
        }
        
        try:
            response = requests.post(
                f"{self.base_url}/routine/exercises/{exercise_id}/add-media/",
                json=media_data,
                headers=self.headers
            )
            
            if response.status_code in [200, 201, 207]:
                data = response.json()
                print("✅ Mixed media added successfully!")
                print(f"   Message: {data.get('message', 'N/A')}")
                print(f"   Created media: {len(data.get('created_media', []))}")
                
                created_media = data.get('created_media', [])
                for media in created_media:
                    print(f"   - {media['media_type'].upper()}: {media['title']}")
                
                return data
            else:
                print(f"❌ Failed to add mixed media: {response.status_code}")
                print(f"Response: {response.text}")
                return None
        except Exception as e:
            print(f"❌ Error adding mixed media: {e}")
            return None

def main():
    print("🏋️ Exercise Add Media API Test")
    print("=" * 50)
    
    tester = ExerciseAddMediaTester()
    
    # Authenticate
    if not tester.authenticate():
        print("❌ Cannot proceed without authentication")
        return
    
    # Get exercises
    exercises = tester.get_exercises()
    if not exercises:
        print("❌ No exercises found")
        return
    
    # Use the first exercise for testing
    exercise = exercises[0]
    exercise_id = exercise['id']
    exercise_name = exercise['name']
    
    print(f"\n🎯 Using exercise: {exercise_name} (ID: {exercise_id})")
    
    # Test 1: Add video media
    tester.add_video_media(exercise_id)
    
    # Test 2: Add photo media
    tester.add_photo_media(exercise_id)
    
    # Test 3: Add text media
    tester.add_text_media(exercise_id)
    
    # Test 4: Get all media
    media_data = tester.get_exercise_media(exercise_id)
    
    # Test 5: Test mixed media
    tester.test_mixed_media(exercise_id)
    
    # Test 6: Delete some media (if we have media to delete)
    if media_data and media_data.get('media_items'):
        media_items = media_data['media_items']
        if len(media_items) >= 2:
            media_ids_to_delete = [media_items[0]['id'], media_items[1]['id']]
            tester.delete_media(exercise_id, media_ids_to_delete)
    
    # Final check
    print(f"\n📊 Final media count for exercise {exercise_name}:")
    final_media = tester.get_exercise_media(exercise_id)
    
    print("\n✅ Exercise Add Media API Test Complete!")
    print("\n📋 Summary of available endpoints:")
    print("   POST   /api/routine/exercises/{id}/add-media/  - Add media items")
    print("   GET    /api/routine/exercises/{id}/add-media/  - Get all media")
    print("   DELETE /api/routine/exercises/{id}/add-media/  - Delete media items")

if __name__ == "__main__":
    main() 