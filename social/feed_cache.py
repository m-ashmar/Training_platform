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

def remove_post_from_feeds(post_id, follower_ids=None):
    """Drop a post id from the global stream and from the feeds that carry it.

    Nothing ever removed an id: a grep for `zrem` found only the `zremrangebyrank` that
    trims each key to 500. So a deleted post — or an erased author's, or one that became
    invisible — stayed in every follower's sorted set for the key's 24-hour life, and the
    reader silently dropped it after the SQL visibility filter. One hole ended the feed:
    a page came back four rows instead of five, `has_more` went False, and everything
    behind it was unreachable until the key expired.

    `has_more` now counts the ZSET rather than the survivors, so a hole no longer ends
    the feed; this keeps the holes from accumulating in the first place.
    """
    client = get_redis_client()
    if not client:
        return
    member = str(post_id)
    keys = ["feed_global:v1"]
    for follower_id in (follower_ids or []):
        keys.append(f"feed:v1:{follower_id}")
    for key in keys:
        try:
            client.zrem(key, member)
        except Exception as exc:
            logger.warning("Could not remove post %s from %s: %s", post_id, key, exc)


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
