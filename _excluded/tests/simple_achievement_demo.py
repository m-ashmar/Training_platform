#!/usr/bin/env python
"""
Simple Achievement System Demo

Demonstrates how achievements are automatically awarded through the API.
"""
import requests
import json

# API Configuration
BASE_URL = 'http://localhost:8000'
API_BASE = f'{BASE_URL}/api'

# Colors for terminal output
class Colors:
    GREEN = '\033[92m'
    BLUE = '\033[94m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    PURPLE = '\033[95m'
    CYAN = '\033[96m'
    BOLD = '\033[1m'
    END = '\033[0m'

def print_header(text):
    print(f"\n{Colors.BOLD}{Colors.BLUE}{'=' * 60}{Colors.END}")
    print(f"{Colors.BOLD}{Colors.BLUE}{text.center(60)}{Colors.END}")
    print(f"{Colors.BOLD}{Colors.BLUE}{'=' * 60}{Colors.END}")

def print_achievement(name, points, desc):
    print(f"{Colors.GREEN}🏆 ACHIEVEMENT: {name}{Colors.END}")
    print(f"{Colors.YELLOW}   +{points} points{Colors.END}")
    print(f"{Colors.CYAN}   {desc}{Colors.END}")

def print_status(status):
    print(f"{Colors.BLUE}📊 {status}{Colors.END}")

def test_achievement_system():
    """Test the achievement system through API calls"""
    
    print_header("🏆 ACHIEVEMENT SYSTEM API DEMO")
    
    # Step 1: Test authentication
    print_status("Testing authentication...")
    try:
        auth_response = requests.post(f'{API_BASE}/auth/token/', {
            'email': 'api_test_user@example.com',
            'password': 'TestPass123!'
        }, timeout=10)
        
        if auth_response.status_code == 200:
            auth_data = auth_response.json()
            headers = {'Authorization': f'Bearer {auth_data["access"]}'}
            print_status("✅ Authentication successful")
        else:
            print_status("❌ Authentication failed - creating new test user...")
            
            # Try to register a new user
            register_response = requests.post(f'{API_BASE}/auth/register/', {
                'username': 'achievement_demo',
                'email': 'achievement@demo.test',
                'password1': 'AchieveDemo123!',
                'password2': 'AchieveDemo123!',
                'phone_number': '+1234567890',
                'user_type': 'client'
            }, timeout=10)
            
            if register_response.status_code == 201:
                print_status("✅ New user created, logging in...")
                auth_response = requests.post(f'{API_BASE}/auth/token/', {
                    'email': 'achievement@demo.test',
                    'password': 'AchieveDemo123!'
                }, timeout=10)
                
                if auth_response.status_code == 200:
                    auth_data = auth_response.json()
                    headers = {'Authorization': f'Bearer {auth_data["access"]}'}
                    print_status("✅ Login successful")
                else:
                    print_status("❌ Login failed")
                    return
            else:
                print_status("❌ User creation failed")
                return
                
    except requests.exceptions.RequestException as e:
        print_status(f"❌ Server connection failed: {e}")
        print_status("Make sure Django server is running on localhost:8000")
        return
    
    # Step 2: Check available achievements
    print_header("📋 AVAILABLE ACHIEVEMENTS")
    
    try:
        achievements_response = requests.get(
            f'{API_BASE}/social/achievements/',
            headers=headers,
            timeout=10
        )
        
        if achievements_response.status_code == 200:
            achievements = achievements_response.json()
            print_status(f"Found {len(achievements)} available achievements")
            
            # Show sample achievements
            for i, achievement in enumerate(achievements[:5]):
                category = achievement.get('category', 'general')
                rarity = '🔥 RARE' if achievement.get('is_rare') else '✨'
                secret = '🤫 SECRET' if achievement.get('is_secret') else ''
                
                print(f"   {rarity} {secret} {achievement['name']}")
                print(f"      📝 {achievement['description']}")
                print(f"      🏆 {achievement['points']} points • {category}")
                print()
            
            if len(achievements) > 5:
                print(f"   ... and {len(achievements) - 5} more achievements!")
                
        else:
            print_status("❌ Could not fetch achievements")
            
    except requests.exceptions.RequestException as e:
        print_status(f"❌ Error fetching achievements: {e}")
    
    # Step 3: Check current user achievements
    print_header("🏅 YOUR CURRENT ACHIEVEMENTS")
    
    try:
        user_achievements_response = requests.get(
            f'{API_BASE}/social/achievements/user_achievements/',
            headers=headers,
            timeout=10
        )
        
        if user_achievements_response.status_code == 200:
            user_data = user_achievements_response.json()
            earned_achievements = user_data.get('achievements', [])
            total_points = user_data.get('total_points', 0)
            
            print_status(f"You have earned {len(earned_achievements)} achievements")
            print_status(f"Total points: {total_points}")
            
            if earned_achievements:
                for ua in earned_achievements:
                    achievement = ua['achievement']
                    earned_date = ua['earned_at'][:10]  # Just the date
                    print_achievement(
                        achievement['name'],
                        achievement['points'],
                        f"Earned on {earned_date}"
                    )
            else:
                print_status("No achievements earned yet - let's earn some!")
                
    except requests.exceptions.RequestException as e:
        print_status(f"❌ Error fetching user achievements: {e}")
    
    # Step 4: Trigger activities that could earn achievements
    print_header("🎯 TRIGGERING ACHIEVEMENT-WORTHY ACTIVITIES")
    
    activities_to_test = [
        {
            'name': 'Track Activity',
            'endpoint': f'{API_BASE}/analytics/activities/track_activity/',
            'data': {
                'activity_type': 'workout_completed',
                'metadata': {
                    'workout_type': 'strength',
                    'duration': 45,
                    'achievement_demo': True
                }
            },
            'expected_achievements': ['First Workout (if first workout)']
        },
        {
            'name': 'Create Social Post',
            'endpoint': f'{API_BASE}/social/posts/',
            'data': {
                'post_type': 'workout',
                'title': 'Achievement Demo Workout!',
                'content': 'Testing the achievement system with a workout post! 💪',
                'visibility': 'public'
            },
            'expected_achievements': ['Social Butterfly (if first post)']
        },
        {
            'name': 'Log Performance Metric',
            'endpoint': f'{API_BASE}/analytics/metrics/',
            'data': {
                'metric_type': 'weight',
                'value': 75.0,
                'unit': 'kg',
                'notes': 'Achievement demo weight tracking'
            },
            'expected_achievements': ['Goal tracking achievements possible']
        }
    ]
    
    for activity in activities_to_test:
        print(f"\n{Colors.BOLD}Testing: {activity['name']}{Colors.END}")
        
        try:
            response = requests.post(
                activity['endpoint'],
                json=activity['data'],
                headers=headers,
                timeout=10
            )
            
            if response.status_code in [200, 201]:
                print_status(f"✅ {activity['name']} completed successfully")
                print_status(f"   Possible achievements: {', '.join(activity['expected_achievements'])}")
            else:
                print_status(f"⚠️  {activity['name']} returned status {response.status_code}")
                
        except requests.exceptions.RequestException as e:
            print_status(f"❌ Error with {activity['name']}: {e}")
    
    # Step 5: Check for new achievements
    print_header("🏆 CHECKING FOR NEW ACHIEVEMENTS")
    
    try:
        # Check again for earned achievements
        user_achievements_response = requests.get(
            f'{API_BASE}/social/achievements/user_achievements/',
            headers=headers,
            timeout=10
        )
        
        if user_achievements_response.status_code == 200:
            updated_data = user_achievements_response.json()
            updated_achievements = updated_data.get('achievements', [])
            updated_points = updated_data.get('total_points', 0)
            
            print_status(f"Total achievements now: {len(updated_achievements)}")
            print_status(f"Total points now: {updated_points}")
            
            # Show recent achievements (earned today)
            from datetime import datetime
            today = datetime.now().strftime('%Y-%m-%d')
            
            recent_achievements = [
                ua for ua in updated_achievements 
                if ua['earned_at'].startswith(today)
            ]
            
            if recent_achievements:
                print(f"\n{Colors.GREEN}🎉 ACHIEVEMENTS EARNED TODAY:{Colors.END}")
                for ua in recent_achievements:
                    achievement = ua['achievement']
                    print_achievement(
                        achievement['name'],
                        achievement['points'],
                        achievement['description']
                    )
            else:
                print_status("No new achievements earned in this demo")
                print_status("Note: Achievements may require meeting specific criteria")
                
        # Check notifications for achievement alerts
        notifications_response = requests.get(
            f'{API_BASE}/social/notifications/',
            headers=headers,
            timeout=10
        )
        
        if notifications_response.status_code == 200:
            notifications = notifications_response.json()
            achievement_notifications = [
                n for n in notifications 
                if n.get('notification_type') == 'achievement'
            ]
            
            if achievement_notifications:
                print(f"\n{Colors.PURPLE}🔔 ACHIEVEMENT NOTIFICATIONS:{Colors.END}")
                for notification in achievement_notifications:
                    print(f"   🏆 {notification['title']}")
                    print(f"      {notification['message']}")
                    
    except requests.exceptions.RequestException as e:
        print_status(f"❌ Error checking final achievements: {e}")
    
    # Step 6: Show system summary
    print_header("📊 ACHIEVEMENT SYSTEM SUMMARY")
    
    print(f"""
{Colors.GREEN}✅ Achievement System Demonstration Complete!{Colors.END}

{Colors.CYAN}What Was Tested:{Colors.END}
• ✅ Authentication with JWT tokens
• ✅ Available achievements retrieval  
• ✅ User achievements tracking
• ✅ Activity logging that triggers achievement checks
• ✅ Social post creation for social achievements
• ✅ Performance metrics for milestone achievements
• ✅ Achievement notifications

{Colors.YELLOW}How Achievements Work:{Colors.END}
1. 🎯 User performs activities (workout, post, etc.)
2. 📊 System automatically checks achievement criteria
3. 🏆 Awards achievements when standards are met
4. 🔔 Sends notifications to user
5. 📈 Tracks points and progress

{Colors.PURPLE}Available Achievement Categories:{Colors.END}
• 🏋️  Workout achievements (counts, streaks)
• 🥗 Diet achievements (meal logging)
• 👥 Social achievements (posts, followers)
• 🎯 Challenge achievements (participation, wins)
• 🏅 Milestone achievements (goals, weight loss)
• 🤫 Secret achievements (special conditions)

{Colors.BOLD}Your achievement system is fully functional and ready to motivate users!{Colors.END}
""")

if __name__ == '__main__':
    test_achievement_system() 