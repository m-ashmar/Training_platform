"""The objective function.

The old pipeline had seven correctors and no shared notion of "better", so a stage could
run, change nothing, and nobody noticed (`MacroShortageBooster` against a 35% fat gap),
or move the plan further from target and nobody noticed either (`CalorieTrimmer` on a
plan already 3.7% under). One measure, used by every stage, removes both.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

MACROS = ("calories", "protein", "carb", "fat")


@dataclass(frozen=True)
class MacroDeviation:
    """Signed relative deviation from target, per macro. 0.0 means exactly on target."""

    calories: float = 0.0
    protein: float = 0.0
    carb: float = 0.0
    fat: float = 0.0

    def as_dict(self) -> Dict[str, float]:
        return {m: getattr(self, m) for m in MACROS}

    def worst(self, tolerance: Dict[str, float]) -> tuple[str, float]:
        """The macro furthest outside its own tolerance band.

        Normalising by each macro's tolerance is what lets protein (0.15) outrank carbs
        (0.20) at the same raw deviation — the tolerances were declared in settings and
        previously read by nothing.
        """
        worst_macro, worst_score = "calories", 0.0
        for macro in MACROS:
            tol = max(tolerance.get(macro, 0.10), 1e-9)
            score = abs(getattr(self, macro)) / tol
            if score > worst_score:
                worst_macro, worst_score = macro, score
        return worst_macro, worst_score

    def within(self, tolerance: Dict[str, float]) -> bool:
        return all(
            abs(getattr(self, m)) <= tolerance.get(m, 0.10) for m in MACROS
        )

    @property
    def magnitude(self) -> float:
        """Single scalar for comparing two candidate plans.

        Calories are deliberately EXCLUDED: energy is a linear function of the three
        macros, so counting it again double-weights every change. With it included, a
        move that correctly cut a +30% carb surplus also moved calories down, the summed
        magnitude failed to improve, the move was rejected and the loop stopped — leaving
        the surplus in place. Optimise the three macros; energy follows, and the
        tolerance check below still guards it.
        """
        return sum(abs(getattr(self, m)) for m in ("protein", "carb", "fat"))

    def human(self) -> str:
        return ", ".join(f"{m} {getattr(self, m):+.1%}" for m in MACROS)


def deviation_of(totals: Dict[str, float], targets: Dict[str, float]) -> MacroDeviation:
    """Relative deviation of achieved totals from target."""
    out = {}
    for macro in MACROS:
        target = float(targets.get(macro, 0.0) or 0.0)
        actual = float(totals.get(macro, 0.0) or 0.0)
        out[macro] = 0.0 if target <= 0 else (actual - target) / target
    return MacroDeviation(**out)
