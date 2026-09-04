"""Re-record the diet quality baseline.

    DIET_RECORD_BASELINE=1 .venv/bin/python -m pytest -q tests/record_diet_baseline.py -s

Measured inside pytest because that is the only place the seeded catalogue exists. The
development database is going to be dropped before launch, so a number taken from it
describes nothing that will ship.
"""
import datetime
import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("DIET_RECORD_BASELINE") != "1",
    reason="set DIET_RECORD_BASELINE=1 to re-record",
)


def test_record(diet_quality, seeded_catalogue):
    payload = {
        "_comment": (
            "Diet engine quality baseline, phase 0.2 of the rebuild. Measured against the "
            "catalogue built by `add_healthy_foods` and `seed_recipes`, which is what a "
            "fresh production database contains. The gate asserts these as bounds in the "
            "direction of improvement and tightens them phase by phase."
        ),
        "recorded": datetime.date.today().isoformat(),
        "catalogue": seeded_catalogue,
        "measured": diet_quality.as_dict(),
    }
    Path("tests/diet_quality_baseline.json").write_text(json.dumps(payload, indent=2) + "\n")
    print("\n" + diet_quality.summary())
