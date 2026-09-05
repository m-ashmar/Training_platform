"""Resolve the curated catalogue's USDA search terms to per-100 g macros.

    python manage.py fetch_usda --api-key YOUR_KEY

Writes `diet/data/usda_cache.json`. Resumable: a food already in the cache is skipped, so
a rate limit or a dropped connection costs nothing. Get a free key in about thirty seconds
at https://fdc.nal.usda.gov/api-key-signup.html — DEMO_KEY allows ten requests per hour,
which is not enough for a catalogue.

Only SR Legacy and Foundation are queried. Those are curated whole foods quoted per 100 g,
which is the convention `FoodItem` already stores. Branded is deliberately excluded: it is
per-serving, crowd-populated, and full of near-duplicate names — the exact problem this
rebuild exists to remove.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests
from django.core.management.base import BaseCommand, CommandError

from diet.data.catalogue import USDA

CACHE = Path(__file__).resolve().parents[2] / "data" / "usda_cache.json"
ENDPOINT = "https://api.nal.usda.gov/fdc/v1/foods/search"

#: FoodData Central nutrient ids. Everything else in the response is discarded.
NUTRIENTS = {1008: "calories", 1003: "protein", 1005: "carbs", 1004: "fat",
             1079: "fiber"}


def _macros(food: dict) -> dict:
    out = {}
    for nutrient in food.get("foodNutrients", []):
        key = NUTRIENTS.get(nutrient.get("nutrientId"))
        if key:
            out[key] = float(nutrient.get("value") or 0.0)
    return out


class Command(BaseCommand):
    help = "Fetch per-100g macros from USDA FoodData Central for the curated catalogue."

    def add_arguments(self, parser):
        parser.add_argument("--api-key", required=True)
        parser.add_argument("--sleep", type=float, default=0.4,
                            help="Seconds between requests. 1000/hour is the keyed limit.")
        parser.add_argument("--refetch", action="store_true",
                            help="Ignore the cache and query every food again.")

    def handle(self, *args, **opts):
        cache = {}
        if CACHE.exists() and not opts["refetch"]:
            cache = json.loads(CACHE.read_text())
            self.stdout.write(f"cache holds {len(cache)} food(s)")

        wanted = [(name, query) for name, query, *_ in USDA if query]
        missing = [(n, q) for n, q in wanted if n not in cache]
        self.stdout.write(f"{len(wanted)} to resolve, {len(missing)} not yet cached")

        failed = []
        for index, (name, query) in enumerate(missing, 1):
            try:
                response = requests.post(
                    ENDPOINT, params={"api_key": opts["api_key"]},
                    json={"query": query,
                          "dataType": ["SR Legacy", "Foundation"],
                          "pageSize": 5},
                    timeout=30)
            except requests.RequestException as exc:
                failed.append((name, f"network: {exc}"))
                continue

            if response.status_code == 429:
                CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
                raise CommandError(
                    f"rate limited after {index - 1} food(s). Cache saved; re-run to resume."
                )
            if response.status_code != 200:
                failed.append((name, f"HTTP {response.status_code}"))
                continue

            foods = response.json().get("foods") or []
            best = next((f for f in foods if _macros(f).get("calories") is not None), None)
            if best is None:
                failed.append((name, "no SR Legacy or Foundation match"))
                continue

            macros = _macros(best)
            cache[name] = {
                "fdc_id": best.get("fdcId"),
                "usda_description": best.get("description"),
                "data_type": best.get("dataType"),
                "query": query,
                **{k: round(macros.get(k, 0.0), 2)
                   for k in ("calories", "protein", "carbs", "fat", "fiber")},
            }
            self.stdout.write(f"  {name:<32} <- {best.get('description','')[:52]}")
            time.sleep(opts["sleep"])

        CACHE.write_text(json.dumps(cache, indent=2, ensure_ascii=False))
        self.stdout.write(self.style.SUCCESS(f"cached {len(cache)} food(s) to {CACHE}"))
        if failed:
            self.stdout.write(self.style.WARNING(f"{len(failed)} unresolved:"))
            for name, why in failed:
                self.stdout.write(f"    {name}: {why}")
            self.stdout.write("Add these to LEVANTINE in diet/data/catalogue.py "
                              "with inline macros, or fix the search term.")
