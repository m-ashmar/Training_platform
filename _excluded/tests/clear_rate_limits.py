#!/usr/bin/env python3
"""
Clear Rate Limits Script
Clears all existing rate limit cache entries to reset limits immediately
"""

import os
import django

# Setup Django
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'training_platform.settings')
django.setup()

from django.core.cache import cache
from django.conf import settings

def clear_rate_limits():
    """Clear all rate limit cache entries"""
    
    print("🧹 Clearing rate limit cache entries...")
    
    try:
        # Get all cache keys that start with 'rate_limit:'
        # Note: This is a simplified approach. In production with Redis,
        # you might want to use SCAN command for better performance
        
        # For now, we'll clear the cache completely if it's Redis
        if hasattr(settings, 'CACHES') and 'default' in settings.CACHES:
            cache_backend = settings.CACHES['default']['BACKEND']
            
            if 'redis' in cache_backend.lower():
                print("📡 Redis cache detected - clearing all cache...")
                cache.clear()
                print("✅ All cache entries cleared")
            else:
                print("💾 Local cache detected - rate limits will reset automatically")
                print("ℹ️  Rate limits are now set to very high values for testing")
        else:
            print("⚠️  No cache configuration found")
            
    except Exception as e:
        print(f"❌ Error clearing cache: {e}")
        print("ℹ️  Rate limits have been updated to high values anyway")
    
    print("\n📊 NEW RATE LIMITS (per hour):")
    print("   • Anonymous: 10,000 requests")
    print("   • Client: 50,000 requests") 
    print("   • Trainer: 100,000 requests")
    print("   • Admin: 500,000 requests")
    print("\n🚀 You can now test APIs without rate limiting issues!")
    print("⚠️  Remember to reset these limits before production deployment!")

if __name__ == "__main__":
    clear_rate_limits() 