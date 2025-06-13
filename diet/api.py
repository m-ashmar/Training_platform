# diet/api.py
import requests
from django.conf import settings

def search_food(query):
    url = "https://api.edamam.com/api/food-database/v2/parser"
    params = {
        'app_id': settings.EDAMAM_APP_ID,
        'app_key': settings.EDAMAM_APP_KEY,
        'ingr': query
    }
    response = requests.get(url, params=params)
    return response.json()