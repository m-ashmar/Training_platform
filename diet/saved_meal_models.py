"""A trainer's saved meals (BACKEND_BUGS B-130; screens/14-trainer-nutrition).

«احفظ في وجباتي» in the meal editor keeps a meal — a name and its foods at their grams —
so the next plan, for any client, is faster; the food picker inserts one whole. It lived
on the phone (`PrefsSavedMealsStore`), so a trainer's library did not follow him to a
new phone. It lives here now, private to the trainer who saved it: no other trainer,
client or route reads it.

A saved meal holds no client data. It is the trainer's own, exported with and erased
with the trainer's account (`training_platform/privacy/sources.py`).

Registered in the `diet` app from `diet/models.py`, beside the foods it is made of.
"""
from django.conf import settings
from django.db import models
from django.utils import timezone

from .models import FINITE_CEILING, FoodItem, Meal


class TrainerSavedMeal(models.Model):
    """One meal in a trainer's library: a name, an optional kind, and its foods."""

    trainer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name='saved_meals')
    #: The trainer's own words, in whatever language they wrote them.
    name = models.CharField(max_length=120)
    #: A hint for the picker, never a rule: a saved breakfast may be served at dinner.
    meal_type = models.CharField(max_length=20, choices=Meal.MEAL_TYPES, blank=True,
                                 default='')
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Most recently used first, and total: `id` breaks every tie.
        ordering = ['-updated_at', '-id']
        indexes = [models.Index(fields=['trainer', '-updated_at', '-id'],
                                name='savedmeal_trainer_recent_idx')]

    def __str__(self):
        return f"{self.trainer_id}: {self.name}"


class TrainerSavedMealItem(models.Model):
    """One food of a saved meal, at its grams, in the order the trainer put it."""

    saved_meal = models.ForeignKey(TrainerSavedMeal, on_delete=models.CASCADE,
                                   related_name='items')
    #: CASCADE, not PROTECT: this is a trainer's shortcut, not anybody's record of what
    #: they ate. `load_food_catalogue --wipe` counts these rows among what a wipe would
    #: erase (`_client_data_referencing_foods`, derived from the model graph) and refuses
    #: without `--force`.
    food = models.ForeignKey(FoodItem, on_delete=models.CASCADE,
                             related_name='saved_meal_items')
    #: Grams, 1-1000: the range every other writer of a portion accepts
    #: (`DietInputValidator.MIN_QUANTITY` / `MAX_QUANTITY`), checked by the view.
    grams = models.FloatField()
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['position', 'id']
        constraints = [
            # One line per food, as on a plan's meal (`duplicate_food`).
            models.UniqueConstraint(fields=['saved_meal', 'food'],
                                    name='savedmealitem_one_line_per_food'),
            # Finite and positive. `> 0` alone admits NaN in Postgres; the upper bound
            # refuses it, as on every other amount in this app.
            models.CheckConstraint(
                condition=models.Q(grams__gt=0, grams__lt=FINITE_CEILING),
                name='savedmealitem_grams_are_a_real_amount'),
        ]

    def __str__(self):
        return f"{self.saved_meal_id}: {self.food_id} {self.grams:g}g"
