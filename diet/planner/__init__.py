"""The diet planning system.

Replaces a single 1,791-line greedy filler with subsystems that can each be reasoned
about and tested on their own:

    policy      every nutrition constant, named and overridable
    targets     TDEE -> daily kcal -> macro targets -> per-meal split
    candidates  catalogue -> hard filters -> preference RANKING
    selection   greedy initial solution (the existing domain knowledge)
    optimize    objective + bounded local search until within tolerance
    recipes     dish assembly, so a meal is food rather than a macro pile
    learning    per-user food weights from what was actually eaten
    report      the deviation, returned with the plan instead of hidden

The old planner deliberately overshot ("10% slack for the first two macros") and relied
on seven downstream correctors to walk it back — a chain that was measured reaching
+4.1% and then degrading to -6.6% before shipping. Here the overshoot is gone and the
correctors become moves in a search with a convergence criterion.
"""
from .candidates import CandidatePool, build_pool  # noqa: F401
from .policy import PlannerPolicy, load_policy  # noqa: F401
from .report import MacroDeviation, deviation_of  # noqa: F401
from .targets import DayTargets, MealTargets, compute_targets  # noqa: F401
from .learning import update_weights  # noqa: F401,E402
from .recipes import RecipeMatch, find_recipe  # noqa: F401,E402
