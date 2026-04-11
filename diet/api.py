# diet/api.py
import hashlib
import requests
from django.conf import settings


def search_food(query: str) -> dict:
    """
    Query the Edamam food database API, with 24h caching (edamam_cache, DB4).
    Cache key is SHA-256 of the lowercased query — prevents PII in Redis keys.
    """
    from training_platform.cache import edamam_cache

    cache_key = "edamam:search:" + hashlib.sha256(query.lower().strip().encode()).hexdigest()
    ec = edamam_cache()

    cached = ec.get(cache_key)
    if cached is not None:
        return cached

    url = "https://api.edamam.com/api/food-database/v2/parser"
    params = {
        'app_id': settings.EDAMAM_APP_ID,
        'app_key': settings.EDAMAM_APP_KEY,
        'ingr': query,
    }
    response = requests.get(url, params=params, timeout=10)
    data = response.json()

    # Only cache valid responses (not error payloads)
    if 'hints' in data:
        ec.set(cache_key, data, timeout=86400)  # 24 hours

    return data