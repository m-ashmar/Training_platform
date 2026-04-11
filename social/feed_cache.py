import logging
from training_platform.cache import private_cache

logger = logging.getLogger(__name__)

def get_redis_client():
    """
    Returns the raw redis-py client from the Django cache backend if using django-redis.
    Returns None if using DummyCache or LocMemCache.
    """
    cache = private_cache()
    try:
        if hasattr(cache, 'client'):
            return cache.client.get_client(write=True)
    except Exception as e:
        logger.warning(f"Could not get raw Redis client: {e}")
    return None

def push_post_to_global_stream(post_id, timestamp):
    """
    Pushes a post to the global stream natively using Redis ZSET.
    Used for accounts with massive followings (>10k) to prevent fan-out queues.
    """
    client = get_redis_client()
    if not client:
        return
    key = "feed_global:v1"
    try:
        client.zadd(key, {str(post_id): timestamp})
        client.zremrangebyrank(key, 0, -501)  # Keep only the latest 500
    except Exception as e:
        logger.error(f"Global feed push failed: {e}")

def get_user_feed(user_id, offset=0, limit=20):
    """
    Fetches the combined hybrid feed by merging the personal ZSET list
    and the global ZSET stream natively ordered by timestamp.
    """
    client = get_redis_client()
    if not client:
        raise Exception("No Redis client available for ZSET manipulation")
        
    user_feed_key = f"feed:v1:{user_id}"
    global_feed_key = "feed_global:v1"
    
    try:
        # Request a larger slice of global based on offset to ensure overlapping values can be sorted
        personal_items = client.zrevrange(user_feed_key, 0, offset + limit, withscores=True)
        global_items = client.zrevrange(global_feed_key, 0, offset + limit, withscores=True)
        
        # Merge lists via dictionary to instantly deduplicate (if any overlap exists)
        merged = {}
        for pid, score in personal_items:
            merged[int(pid)] = score
        for pid, score in global_items:
            merged[int(pid)] = score
            
        # Sort merged dict by score descending
        sorted_pairs = sorted(merged.items(), key=lambda x: x[1], reverse=True)
        
        # Paginate the merged list precisely targeting the offset chunk
        paginated_pairs = sorted_pairs[offset:offset+limit]
        return [pid for pid, score in paginated_pairs]
    except Exception as e:
        logger.error(f"ZSET merge failed: {e}")
        raise e
