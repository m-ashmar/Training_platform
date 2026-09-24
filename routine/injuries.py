"""Which exercises an injury argues with (BACKEND_BUGS B-105; TrainerDayEditor's amber
chip: «سامر: إصابة في الكتف الأيمن — تجنّب الضغط العلوي الثقيل»).

The map is from a client's `injury_areas` code (users/models.py `INJURY_AREAS`) to the
`Exercise.target_muscle` keys whose exercises load that joint DIRECTLY. It is narrow on
purpose, and the artboard is the reason: a shoulder injury flags the overhead press
(Front Deltoid) and NOT the bench press, the lateral raise or the incline fly drawn on
the same day. A chip on every exercise near a joint is a chip the trainer learns to
ignore. The one muscle per area is the movement a physiotherapist names first:

  shoulder    Front Deltoid                  overhead pressing
  lower_back  Lower Back                     deadlifts, good mornings, back extensions
  knee        Front Quads                    squats, lunges, leg extensions
  neck        Neck, Traps                    neck work; shrugs load the cervical spine
  wrist       Forearms                       wrist curls and extensions
  ankle       Calves                         calf raises
  hip         Glutes, Adductors, Abductors   hip thrusts, hip adduction and abduction
  other       (none)                         a condition the client describes in words

It is a WARNING for the trainer, never a refusal: nothing here stops a row being
written, and the trainer, who has seen the client, decides.

No lighter-load number is served. The app's local table suggested 85 % of the load;
nothing in the platform's data or the design justifies one figure for every injury,
exercise and client, and a number printed by the server reads as advice. The trainer
chooses the load (owner to confirm; see `BACKEND_BUGS.md` B-105).

`INJURY_MAP_VERSION` goes up whenever the map changes, so the app can keep a copy.
"""
from __future__ import annotations

INJURY_MAP_VERSION = 1

INJURY_MUSCLES = {
    'shoulder': ['Front Deltoid'],
    'lower_back': ['Lower Back'],
    'knee': ['Front Quads'],
    'neck': ['Neck', 'Traps'],
    'wrist': ['Forearms'],
    'ankle': ['Calves'],
    'hip': ['Glutes', 'Adductors', 'Abductors'],
    'other': [],
}


def injury_map() -> dict:
    """The served map: every injury area, in `INJURY_AREAS` order, with its muscles."""
    from users.models import INJURY_AREAS

    return {
        'version': INJURY_MAP_VERSION,
        'areas': [{'code': code, 'muscles': list(INJURY_MUSCLES.get(code, []))}
                  for code, _label in INJURY_AREAS],
    }
