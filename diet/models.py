"""
models.py - Database Models for Diet App

This module defines the database schema for food items, categories, user preferences,
diet plans, meals, meal components, and daily advice.
Enhanced to support both AI-generated and trainer-created diet plans.
"""

from django.db import models
from users.models import CustomUser
from datetime import date, datetime
from django.utils import timezone
from django.core.validators import MinValueValidator, MaxValueValidator
import logging
from training_platform.model_validation import RowValidationMixin

logger = logging.getLogger(__name__)

class FoodCategory(models.Model):
    """
    Represents a category of food (e.g., Proteins, Carbs, Fats) and its meal time association.
    """
    MEAL_TIME_CHOICES = [
        ('ANY', 'Any Time'),
        ('BREAKFAST', 'Breakfast'),
        ('LUNCH', 'Lunch'),
        ('DINNER', 'Dinner'),
    ]
    # Unique: four call sites `get_or_create` on this name, and without a database
    # constraint two concurrent imports create two 'Other' rows. Everything that
    # then resolves a category by name — the macro filters, the admin's
    # `filter(is_protein=True).first()` — picks one of them arbitrarily, so one
    # macro's foods end up split across two categories.
    name = models.CharField(max_length=50, unique=True)

    @classmethod
    def by_canonical_name(cls, label: str, **defaults):
        """Get or create a category by its ENGLISH name.

        `name` is a modeltranslation virtual field, so `filter(name=...)` and
        `get_or_create(name=...)` both read whatever column the ACTIVE language
        selects. Under an Arabic request they looked at `name_ar`, found nothing, and
        created a second row whose `name_en` already existed — which is how one macro's
        foods ended up split across two categories. Identity is the English column.
        """
        from django.db import IntegrityError, transaction

        def find():
            # TWO queries, because one cannot ask both questions. `Q(name=label)` looks
            # like it reads the base column and does not: modeltranslation rewrites the
            # lookup key to the active language, so `Q(name_en=...) | Q(name=...)` was
            # `name_en` OR `name_en`, and a row written before the translation columns
            # existed — `name='Proteins'`, `name_en=NULL` — was invisible to it. The
            # INSERT that followed then hit `UNIQUE (name)`, the except branch re-ran the
            # same blind query, and this returned None. `load_food_catalogue` assigned
            # that None as the category: 86 of 136 foods came out of a rebuild with no
            # category at all, and `classify_food` fell back to guessing from macros.
            # `.rewrite(False)` is modeltranslation's own escape hatch to the real column.
            return (cls.objects.filter(name_en=label).first()
                    or cls.objects.rewrite(False).filter(name=label).first())

        existing = find()
        if existing is not None:
            return existing, False
        payload = dict(defaults)
        payload.setdefault("name_en", label)
        try:
            with transaction.atomic():
                return cls.objects.create(name=label, **payload), True
        except IntegrityError:
            # Someone else created it between the read and the write — or a row exists
            # that `find()` could not see, which is now a fault rather than a silent None.
            found = find()
            if found is None:
                raise
            return found, False
    meal_times = models.CharField(max_length=20, choices=MEAL_TIME_CHOICES, default='ANY')
    is_protein = models.BooleanField(default=False)
    is_carb = models.BooleanField(default=False)
    is_fat = models.BooleanField(default=False)
    def __str__(self):
        return f"{self.name} ({self.get_meal_times_display()})"

    class Meta:
        # Deterministic total order. Without it Postgres returns rows in whatever order it
        # likes and LIMIT/OFFSET paging silently repeats and hides rows between pages.
        #
        # `name_en`, not `name`. `name` is a modeltranslation virtual field and
        # modeltranslation rewrites ordering keys through the ACTIVE language, so this
        # read `ORDER BY name_ar` for an Arabic request. Every consumer of the order
        # then changed with the locale: the planner samples recipes with
        # `random.choices` over the population as ordered, so the same client with the
        # same seed on the same day got a different dish from the Arabic app than from
        # the English one. An engine's output must not depend on the display language.
        ordering = ['name_en', 'id']

class FoodItem(RowValidationMixin, models.Model):
    """
    Represents a food item with nutritional information and category.
    """

    #: What a food is *for* in a meal, which is not the same question as which macro it
    #: leads on. Ranking by grams of macro per kcal is maximised by things that are
    #: almost pure macro and nothing else, so BBQ sauce and mint jelly outranked rice as
    #: a carbohydrate. Only a staple may be a slot's primary source.
    ROLE_STAPLE = "staple"
    ROLE_ACCOMPANIMENT = "accompaniment"
    ROLE_CONDIMENT = "condiment"
    ROLE_CHOICES = [
        (ROLE_STAPLE, "Staple — a meal can be built on it"),
        (ROLE_ACCOMPANIMENT, "Accompaniment — served alongside, gram-capped"),
        (ROLE_CONDIMENT, "Condiment — added by the spoon, never a primary source"),
    ]

    api_id = models.CharField(max_length=255, unique=True)
    name = models.CharField(max_length=255)
    image_url = models.URLField(max_length=500, blank=True, null=True)
    calories = models.FloatField()
    protein = models.FloatField()
    carbs = models.FloatField()
    fat = models.FloatField()
    serving_size = models.CharField(max_length=100)
    category = models.ForeignKey(FoodCategory, on_delete=models.SET_NULL, null=True, blank=True)
    serving_size_grams = models.PositiveIntegerField(default=100)
    # 0.0 like its three siblings. A default of 1.0 meant every new FoodItem created
    # with calories=0 hit the reverse-initialisation below and was stored as 100 kcal,
    # so a zero-calorie food (water, black coffee, diet soda) could not be entered at all.
    calories_per_gram = models.FloatField(default=0.0)
    protein_per_gram = models.FloatField(default=0.0)
    carbs_per_gram = models.FloatField(default=0.0)
    fat_per_gram = models.FloatField(default=0.0)
    smart_score_weight = models.FloatField(default=1.0, help_text="Adaptive weight for smart macro planner")


    # --- Allergen awareness -------------------------------------------------
    # A Meal is composed of MealComponents pointing at FoodItems, so the FoodItem IS
    # the ingredient. Allergen safety therefore belongs here, not in a name match.
    allergens = models.JSONField(
        default=list, blank=True,
        help_text="Canonical allergen tags from diet.allergens.ALLERGENS. Never free text.",
    )
    ALLERGEN_SOURCE_CHOICES = [
        ('verified', 'Verified / curated'),
        ('inferred', 'Inferred from name (unverified hint)'),
        ('unknown', 'No allergen data'),
    ]
    allergen_source = models.CharField(
        max_length=16, choices=ALLERGEN_SOURCE_CHOICES, default='unknown', db_index=True,
        help_text="Trust level of `allergens`. 'unknown' must NEVER be treated as safe.",
    )
    needs_review = models.BooleanField(
        default=False,
        help_text=(
            "Nutrition on this row failed a sanity check and a human has not yet "
            "corrected it. The planner will not portion from it."
        ),
    )
    # --- how much of this a person actually serves ---------------------------
    # A portion used to be an unbounded continuous gram figure, because nothing in the
    # catalogue said what a serving was: `serving_size` reads "100g" for 339 of 340 rows
    # and is the basis the macros are quoted against, not a portion. So the planner
    # produced 350 g of egg white, which is eleven of them, and 370 g of squash.
    # Expressing a portion as a multiple of a unit a person recognises makes those
    # unrepresentable rather than merely discouraged, and it stops a minimum from
    # becoming a permanent answer the way a post-hoc floor does.
    household_unit = models.CharField(
        max_length=32, blank=True,
        help_text="What one of these is to a person: egg, slice, cup, tbsp, medium, handful.",
    )
    unit_grams = models.FloatField(
        null=True, blank=True,
        help_text="Grams in one household_unit. Null means unknown; the planner then "
                  "falls back to grams and caps the portion.",
    )
    min_units = models.FloatField(
        default=0.5,
        help_text="Fewest units worth serving. A minimum inside the search space, not a "
                  "floor applied afterwards.",
    )
    max_units = models.FloatField(
        null=True, blank=True,
        help_text="Most units a person would serve at one meal. Null means unknown.",
    )
    role = models.CharField(
        max_length=16, choices=ROLE_CHOICES, default=ROLE_STAPLE, db_index=True,
        help_text="Whether a meal can be built on this food, served alongside it, or "
                  "only seasoned with it.",
    )
    #: Which meals this food suits. Meal-appropriateness was derived entirely from the
    #: recipe library, so it existed only for the 16 foods that appeared in a recipe and
    #: the other 85 had no signal at all — which is how a breakfast came out as grilled
    #: chicken, white rice and olive oil. A food can say this about itself.
    meal_slots = models.JSONField(
        default=list, blank=True,
        help_text="Meals this food suits, e.g. ['Breakfast', 'Snack']. Empty means any.")
    CUISINE_UNIVERSAL, CUISINE_WESTERN, CUISINE_LEVANTINE = 'universal', 'western', 'levantine'
    CUISINE_CHOICES = [(CUISINE_UNIVERSAL, 'Universal'), (CUISINE_WESTERN, 'Western'),
                       (CUISINE_LEVANTINE, 'Levantine')]
    #: Which table this food belongs on. Universal is the default and means both: a
    #: chicken breast is not Western or Levantine, it is dinner.
    cuisine = models.CharField(max_length=12, choices=CUISINE_CHOICES,
                               default=CUISINE_UNIVERSAL, db_index=True)
    ingredients_text = models.TextField(
        blank=True, default='',
        help_text="Raw ingredient list, when the source provides one. Scanned for allergens.",
    )
    def save(self, *args, **kwargs):
        """
        Normalize macros to 100g and auto-calculate per-gram values on save.
        
        This ensures all foods store macros per 100g for consistency:
        - If serving_size_grams != 100, normalize macros to 100g
        - Always calculate per-gram values from normalized 100g values
        """
        from .validators import DietInputValidator
        from django.core.exceptions import ValidationError
        from django.utils.translation import gettext_lazy as _
        
        # Cast to float in case incoming values are strings
        
        # Hydrate missing api_id to prevent Unique Constraint violations in tests and ad-hoc generation
        if not self.api_id:
            import uuid
            self.api_id = uuid.uuid4().hex
            
        try:
            self.calories = float(self.calories)
        except Exception:
            self.calories = 0.0
        try:
            self.protein = float(self.protein)
        except Exception:
            self.protein = 0.0
        try:
            self.carbs = float(self.carbs)
        except Exception:
            self.carbs = 0.0
        try:
            self.fat = float(self.fat)
        except Exception:
            self.fat = 0.0
            
        # Reverse initialisation: derive the per-100g macros from per-gram values when a
        # caller supplied only those. ON CREATE ONLY. `0` is being used as a sentinel for
        # "not provided", which is indistinguishable from a real zero, so on UPDATE this
        # silently restored the previous value: correcting a 200 kcal drink to 0 kcal and
        # saving put 200 kcal straight back. On an update the caller's value is the truth.
        if self._state.adding:
            if self.calories == 0.0 and (getattr(self, 'calories_per_gram', 0.0) or 0.0) > 0:
                self.calories = self.calories_per_gram * 100.0
            if self.protein == 0.0 and (getattr(self, 'protein_per_gram', 0.0) or 0.0) > 0:
                self.protein = self.protein_per_gram * 100.0
            if self.carbs == 0.0 and (getattr(self, 'carbs_per_gram', 0.0) or 0.0) > 0:
                self.carbs = self.carbs_per_gram * 100.0
            if self.fat == 0.0 and (getattr(self, 'fat_per_gram', 0.0) or 0.0) > 0:
                self.fat = self.fat_per_gram * 100.0
        
        # BUG FIX: Validate nutritional values are non-negative
        if self.calories < 0:
            raise ValidationError(_("Calories cannot be negative"), code="negative_calories")
        if self.protein < 0:
            raise ValidationError(_("Protein cannot be negative"), code="negative_protein")
        if self.carbs < 0:
            raise ValidationError(_("Carbs cannot be negative"), code="negative_carbs")
        if self.fat < 0:
            raise ValidationError(_("Fat cannot be negative"), code="negative_fat")
        
        # A non-positive serving size used to be silently replaced with 100 g. Every
        # per-gram figure the planner portions from is derived from this number, so a
        # guess must not pass as data. clean() rejects it; this keeps the arithmetic
        # below safe for callers that bypass validation.
        if self.serving_size_grams is None or self.serving_size_grams <= 0:
            self.serving_size_grams = 100
        
        # NORMALIZE: Convert all macros to per-100g standard
        # If serving_size_grams != 100, normalize the macro values to 100g
        if self.serving_size_grams != 100:
            # Calculate normalization factor (how many 100g servings in current serving)
            normalization_factor = 100.0 / self.serving_size_grams
            
            # Normalize macros to 100g
            self.calories = self.calories * normalization_factor
            self.protein = self.protein * normalization_factor
            self.carbs = self.carbs * normalization_factor
            self.fat = self.fat * normalization_factor
            
            # Update serving_size_grams to 100g standard
            self.serving_size_grams = 100
            
            # Update serving_size string to reflect 100g standard
            if not self.serving_size or '100g' not in self.serving_size.lower():
                self.serving_size = '100g'
        
        # Calculate per-gram values from normalized 100g values
        # Since serving_size_grams is now always 100, this is: macro_value / 100
        self.calories_per_gram = self.calories / 100.0
        self.protein_per_gram = self.protein / 100.0
        self.carbs_per_gram = self.carbs / 100.0
        self.fat_per_gram = self.fat / 100.0
        
        self.validate_row()
        super().save(*args, **kwargs)
    def __str__(self):
        return self.name

    class Meta:
        # Deterministic total order. Without it Postgres returns rows in whatever order it
        # likes and LIMIT/OFFSET paging silently repeats and hides rows between pages.
        #
        # `name_en`, not `name`. `name` is a modeltranslation virtual field and
        # modeltranslation rewrites ordering keys through the ACTIVE language, so this
        # read `ORDER BY name_ar` for an Arabic request. Every consumer of the order
        # then changed with the locale: the planner samples recipes with
        # `random.choices` over the population as ordered, so the same client with the
        # same seed on the same day got a different dish from the Arabic app than from
        # the English one. An engine's output must not depend on the display language.
        ordering = ['name_en', 'id']
        constraints = [
            # Every nutrition figure in the app is
            # `quantity / serving_size_grams * per 100 g`. The field is a
            # PositiveIntegerField, so 0 is legal, and `save()` coerces it to 100 — but
            # `queryset.update()`, `bulk_update` and a fixture all bypass `save()`. One
            # 0-gram row anywhere in a plan raised ZeroDivisionError in four endpoints,
            # each of which reported it as a 400: the plan became permanently unreadable
            # and it read as the client's mistake.
            models.CheckConstraint(condition=models.Q(serving_size_grams__gt=0),
                                   name="serving_size_is_a_real_amount"),
        ]

    # Physical ceilings. Nothing validated nutrition before, so the catalogue contains
    # `Cheese, Brick` at 12.00 kcal/g — above the 9 kcal/g maximum for pure fat — and
    # four foods whose macros disagree with their stated calories by more than 35%.
    #: Below this absolute gap the percentage is noise, not a data error. See clean().
    ATWATER_MIN_ABS_KCAL = 25.0
    MAX_KCAL_PER_GRAM = 9.1
    ATWATER_TOLERANCE = 0.35

    def clean(self):
        """Reject nutrition that cannot physically be correct."""
        from django.core.exceptions import ValidationError

        errors = {}
        for field in ('calories', 'protein', 'carbs', 'fat'):
            val = getattr(self, field, None)
            if val is not None and val < 0:
                errors[field] = 'Cannot be negative.'

        grams = self.serving_size_grams or 0
        cal = float(self.calories or 0)
        if grams > 0 and cal > 0 and not self.needs_review:
            per_gram = cal / grams
            if per_gram > self.MAX_KCAL_PER_GRAM:
                errors['calories'] = (
                    f'{per_gram:.2f} kcal/g exceeds the physical maximum of '
                    f'{self.MAX_KCAL_PER_GRAM} (pure fat). Check the serving size.'
                )

        # A serving size with no number in it ("Serving", "Whole") left
        # serving_size_grams sitting on its default of 100 — 32 of 346 catalogue rows,
        # plus 20 with no serving size at all. Every per-gram figure the planner
        # portions from is derived from that number, so a guess must not pass silently.
        text = (self.serving_size or '').strip()
        if not text:
            errors['serving_size'] = 'Required — the planner portions by gram.'
        elif not any(ch.isdigit() for ch in text) and (self.serving_size_grams or 100) == 100:
            errors['serving_size'] = (
                f'{text!r} has no weight in it and serving_size_grams is still the '
                f'default 100 g. Set the real weight, or the macros are a guess.'
            )

        p_, c_, f_ = float(self.protein or 0), float(self.carbs or 0), float(self.fat or 0)
        atwater = 4 * p_ + 4 * c_ + 9 * f_
        # A percentage gap on a small number is not information. Atwater factors apply to
        # AVAILABLE carbohydrate, while every reference table quotes carbohydrate by
        # difference, which includes fibre at roughly 2 kcal/g rather than 4. On a
        # low-calorie plant food that systematically over-counts: a lemon is 29 kcal and
        # estimates at 44, a 52% gap and a 15 kcal one. The absolute floor keeps the check
        # doing its real job — catching a row whose macros and calories describe different
        # foods — without rejecting correct reference data.
        if (cal > 0 and atwater > 0 and not self.needs_review
                and abs(atwater - cal) > self.ATWATER_MIN_ABS_KCAL
                and abs(atwater - cal) / cal > self.ATWATER_TOLERANCE):
            errors['calories'] = (
                f'Stated {cal:.0f} kcal but the macros give {atwater:.0f} kcal '
                f'(>{int(self.ATWATER_TOLERANCE * 100)}% apart).'
            )

        if errors:
            raise ValidationError(errors)


class UserFoodPreference(models.Model):
    """
    Stores user-specific food preferences, allergies, and macro choices.
    """
    # One row per client. This was a plain ForeignKey read everywhere with
    # `.filter(user=).first()`, so a second row — which nothing prevented — silently
    # zeroed every like, dislike and allergy the client had entered.
    user = models.OneToOneField(CustomUser, on_delete=models.CASCADE)
    liked_foods = models.ManyToManyField(FoodItem, related_name='liked_by', blank=True)
    disliked_foods = models.ManyToManyField(FoodItem, related_name='disliked_by', blank=True)
    allergies = models.TextField(blank=True)
    protein_choices = models.ManyToManyField(FoodItem, related_name='protein_prefs', limit_choices_to={'category__name': 'Proteins'})
    carb_choices = models.ManyToManyField(FoodItem, related_name='carb_prefs', limit_choices_to={'category__name': 'Carbs'})
    fat_choices = models.ManyToManyField(FoodItem, related_name='fat_prefs', limit_choices_to={'category__name': 'Fats'})
    vegetable_choices = models.ManyToManyField(FoodItem, related_name='vegetable_prefs', blank=True)
    fruit_choices = models.ManyToManyField(FoodItem, related_name='fruit_prefs', blank=True)
    #: How much of the plan should be local food. 0.0 is Western only, 1.0 is Levantine
    #: only, anything between is a mix at that ratio. Universal foods — chicken, rice,
    #: eggs, vegetables, fruit — are always eligible. At exactly 0 or 1 the other cuisine
    #: is excluded outright; between, it is weighted. The client chooses; the engine
    #: honours it on both selection paths.
    local_ratio = models.FloatField(default=0.5)

    class Meta:
        # Deterministic total order. Without it Postgres returns rows in whatever order it
        # likes and LIMIT/OFFSET paging silently repeats and hides rows between pages.
        ordering = ['-id']


class UserFoodCategoryPreference(models.Model):
    """Per-user meal categorization for liked foods (e.g., Oats → Breakfast Carb)."""
    MEAL_CHOICES = [
        ('Breakfast', 'Breakfast'),
        ('Lunch', 'Lunch'),
        ('Dinner', 'Dinner'),
        ('Snack', 'Snack'),
    ]
    MACRO_CHOICES = [
        ('carb', 'Carb'),
        ('protein', 'Protein'),
        ('fat', 'Fat'),
        ('vegetable', 'Vegetable'),
        ('fruit', 'Fruit'),
    ]
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='food_meal_categories')
    food = models.ForeignKey(FoodItem, on_delete=models.CASCADE)
    meal = models.CharField(max_length=16, choices=MEAL_CHOICES)
    macro = models.CharField(max_length=16, choices=MACRO_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        # Deterministic total order. Without it Postgres returns rows in whatever order it
        # likes and LIMIT/OFFSET paging silently repeats and hides rows between pages.
        ordering = ['-created_at', '-id']
        # (user, food) was wrong: this model carries `meal` AND `macro`, and the planner
        # builds a per-meal-per-macro pool from them — so one food could occupy exactly
        # ONE slot, and chicken could not be both lunch and dinner protein. Measured:
        # 5 of 20 (meal x macro) cells empty even for a fully configured user.
        unique_together = [('user', 'food', 'meal', 'macro')]
        indexes = [
            models.Index(fields=['user', 'meal']),
            models.Index(fields=['user', 'macro']),
        ]

    def __str__(self):
        return f"{self.user_id}:{self.food.name}→{self.meal} {self.macro}"

class MealSwap(models.Model):
    """A client rejected one food on a plate and took another.

    The strongest preference signal a client will ever give, and it is free: nothing
    is inferred, they said so. Learning reads it before anything else.
    """
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='meal_swaps')
    meal = models.ForeignKey('Meal', on_delete=models.CASCADE, related_name='swaps')
    # PROTECT: a swap is the strongest preference signal a client ever gives, and
    # learning reads it. Deleting a food erased the evidence that they refused it.
    rejected_food = models.ForeignKey(FoodItem, on_delete=models.PROTECT, related_name='rejected_in_swaps')
    chosen_food = models.ForeignKey(FoodItem, on_delete=models.PROTECT, related_name='chosen_in_swaps')
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    #: Cosine similarity of rejected -> chosen at the moment of the swap. High means the
    #: food was fine and the client wanted a variant; low means the meal's shape was
    #: wrong for them. Stored at write time so the report does not depend on the
    #: catalogue the index was built from. Reported, not targeted.
    similarity = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ['-at', '-id']
        indexes = [models.Index(fields=['user', 'at'])]

    def __str__(self):
        return f"{self.user_id}: {self.rejected_food_id} -> {self.chosen_food_id}"


class MealEvent(models.Model):
    """One step of the funnel: generated -> viewed -> accepted -> completed -> rated -> swapped.

    Generated is the meal row itself. Viewed and accepted are the client's to emit;
    completed, rated and swapped are recorded by the endpoints that do them. A report
    reads this table; nothing in the planner does.
    """
    VIEWED, ACCEPTED, COMPLETED, RATED, SWAPPED = 'viewed', 'accepted', 'completed', 'rated', 'swapped'
    KINDS = [(VIEWED, 'Viewed'), (ACCEPTED, 'Accepted'), (COMPLETED, 'Completed'),
             (RATED, 'Rated'), (SWAPPED, 'Swapped')]
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='meal_events')
    meal = models.ForeignKey('Meal', on_delete=models.CASCADE, related_name='events')
    kind = models.CharField(max_length=12, choices=KINDS)
    at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-at', '-id']
        indexes = [models.Index(fields=['user', 'kind', 'at'])]

    def __str__(self):
        return f"{self.user_id}: meal {self.meal_id} {self.kind}"


class UserFoodWeight(models.Model):
    """A learned per-client weight for one food.

    `FoodItem.smart_score_weight` was a single global column that the nightly task wrote
    per client, so every client overwrote every other and one person refusing lentils
    twice lowered lentils for the whole platform. Learning that is not per client is
    not personalisation.
    """
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='food_weights')
    food = models.ForeignKey(FoodItem, on_delete=models.CASCADE, related_name='user_weights')
    weight = models.FloatField(default=1.0)
    observations = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Deterministic total order, like every other model here.
        ordering = ['-updated_at', '-id']
        unique_together = [('user', 'food')]
        # No `Index(fields=['user'])`: the ForeignKey indexes it, and `unique_together`
        # adds a (user, food) index whose leading column is the same one.

    def __str__(self):
        return f"{self.user_id}:{self.food_id}={self.weight:.2f}"


class DietPlanTemplate(models.Model):
    """
    Predefined diet plan templates for trainer-created plans.
    """
    name = models.CharField(max_length=100, help_text="Template name (e.g., '3 Meals + 1 Snack')")
    description = models.TextField(blank=True)
    meals_per_day = models.PositiveIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(6)],
        help_text="Number of meals per day (1-6)"
    )
    snacks_per_day = models.PositiveIntegerField(
        validators=[MinValueValidator(0), MaxValueValidator(3)],
        default=0,
        help_text="Number of snacks per day (0-3)"
    )
    days_variation = models.PositiveIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(7)],
        default=1,
        help_text="How often the meal plan repeats (1-7 days)"
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['meals_per_day', 'snacks_per_day', 'id']
    
    def __str__(self):
        return f"{self.name} ({self.meals_per_day}M/{self.snacks_per_day}S - {self.days_variation}D)"
    
    @property
    def total_meals_per_cycle(self):
        """Total meals in one complete cycle."""
        return (self.meals_per_day + self.snacks_per_day) * self.days_variation

class DietPlan(RowValidationMixin, models.Model):
    """
    Represents a generated diet plan for a user, including goal, calories, and plan data.
    Enhanced to support both AI-generated and trainer-created plans.
    """
    # `related_name`, because the default accessor is `dietplan` and the code that
    # reads it back had already guessed `diet_plans`: the nightly advice sweep
    # filtered `diet_plans__is_active=True` and raised FieldError on its first
    # query, so daily advice has never been generated for anyone on the scheduled
    # path. Naming the relation the way it reads is the fix; guessing was the bug.
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE,
                             related_name='diet_plans')
    GOAL_CHOICES = [
        ('Lose', 'Lose Weight'),
        ('Maintain', 'Maintain Weight'),
        ('Gain', 'Gain Muscle')
    ]

    @classmethod
    def normalise_goal(cls, value, default='Maintain'):
        """Map any spelling of a goal onto `GOAL_CHOICES`.

        The planner package works in lower case throughout and this column stores
        title case, so a value that made a whole round trip through the planner came
        back rejected by its own model. Callers taking a goal from request data or
        from the planner run it through here first.
        """
        if value is None:
            return default
        text = str(value).strip().lower()
        for stored, _label in cls.GOAL_CHOICES:
            if text == stored.lower():
                return stored
        return default
    goal = models.CharField(max_length=20, choices=GOAL_CHOICES)
    daily_calories = models.FloatField()
    #: The macro targets this plan was built to, in grams. Six sites re-derived them
    #: from a hardcoded 30/50/20 regardless of goal, so a correctly built Lose plan
    #: rendered as off-target in the app and the wrong numbers were persisted into
    #: DailyProgress for the coach to reason from.
    target_protein = models.FloatField(null=True, blank=True)
    target_carbs = models.FloatField(null=True, blank=True)
    target_fat = models.FloatField(null=True, blank=True)

    def macro_targets(self):
        """Day-level targets in grams: the stored ones, or derived from the goal."""
        if None not in (self.target_protein, self.target_carbs, self.target_fat):
            return {"protein": float(self.target_protein), "carbs": float(self.target_carbs),
                    "fat": float(self.target_fat)}
        from diet.planner.policy import load_policy
        from diet.planner.targets import day_macro_grams
        d = day_macro_grams(float(self.daily_calories or 0.0), load_policy(self.goal),
                            getattr(self.user, "weight", None))
        return {"protein": d["protein"], "carbs": d["carb"], "fat": d["fat"]}
    start_date = models.DateField()
    end_date = models.DateField(db_index=True)
    duration_weeks = models.PositiveIntegerField(default=4)
    generated_plan = models.JSONField(null=True, blank=True)
    # What the allergen checker concluded about this plan's ingredients.
    # This was computed on every generation and then thrown away: the code assigned it
    # to `diet_plan.allergen_report` with the comment "so callers/serializers can act on
    # it", but no such field existed, so it was an instance attribute that died with the
    # function. A user with declared allergies was served ingredients the checker could
    # not clear and nobody was told — not the user, not their trainer.
    #   {"is_safe": bool,
    #    "violations": [{"food": str, "allergens": [str]}],
    #    "unverified": [str]}
    allergen_report = models.JSONField(default=dict, blank=True)
    #: Who built this plan. There were three values for two producers: 'GPT' (the
    #: default, and what an empty plan got), 'FALLBACK' (what the deterministic engine
    #: actually wrote — a name that describes the engine as a last resort when it is the
    #: only one) and 'TRAINER'. No external model is used and none will be, so the
    #: vocabulary says what is true: the engine, or a person.
    ENGINE = 'ENGINE'
    TRAINER = 'TRAINER'
    generation_strategy = models.CharField(
        max_length=20,
        choices=[(ENGINE, 'Rule-Based Engine'), (TRAINER, 'Trainer Created')],
        default=ENGINE,
    )
    
    # New fields for trainer-created plans
    template = models.ForeignKey(
        DietPlanTemplate, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        help_text="Template used for trainer-created plans"
    )
    created_by = models.ForeignKey(
        CustomUser, 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='created_diet_plans',
        help_text="Trainer who created this plan"
    )
    is_active = models.BooleanField(default=True, help_text="Whether this plan is currently active")
    #: NOT NULL. `ordering = ['-created_at', '-id']` puts NULLs FIRST in Postgres on a
    #: descending sort, so one plan with a null timestamp — a `loaddata` fixture, an
    #: import, a `queryset.update()` — became permanently "the latest" for its owner, and
    #: `dietplan_owner_recent_idx` served that wrong order too. 0029/0031/0033 made it
    #: nullable to get past a migration; nothing needed it to stay that way.
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        # A plan with end_date before start_date used to persist (verified:
        # start=2026-09-06, end=2026-09-01 was accepted), and a user could hold two
        # overlapping plans with nothing deciding which governs a shared day. The
        # generation path was patched for inversion once; admin, import and any other
        # writer bypassed that guard, so the rule belongs in the database.
        constraints = [
            models.CheckConstraint(
                check=models.Q(end_date__gte=models.F('start_date')),
                name='dietplan_end_after_start',
            ),
        ]
        indexes = [
            models.Index(fields=['user', 'is_active']),
            models.Index(fields=['user', 'start_date']),
            models.Index(fields=['is_active']),
            # Matches the real access pattern: WHERE <owner>=? ORDER BY created_at DESC, id DESC.
            # A single-column created_at index cannot serve that; this one can.
            models.Index(fields=['user', '-created_at', '-id'], name='dietplan_owner_recent_idx'),
        ]
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['-created_at', '-id']
    
    @property
    def period(self):
        """
        Smart period detection for the plan.
        """
        days = (self.end_date - self.start_date).days
        return 'weekly' if days == 7 else 'monthly' if days > 14 else 'daily'
    
    @property
    def is_engine_generated(self):
        """Built by the deterministic planner rather than by a person."""
        return self.generation_strategy == self.ENGINE

    @property
    def is_trainer_created(self):
        """Check if this plan was created by a trainer."""
        return self.generation_strategy == self.TRAINER
    
    def calculate_daily_nutrition(self, target_date=None):
        """
        Calculate total nutrition for a specific day.
        """
        if target_date is None:
            target_date = timezone.localdate()
        
        meals = self.meals.filter(date=target_date)
        total_calories = 0
        total_protein = 0
        total_carbs = 0
        total_fat = 0
        
        for meal in meals:
            meal_nutrition = meal.calculate_nutrition()
            total_calories += meal_nutrition['calories']
            total_protein += meal_nutrition['protein']
            total_carbs += meal_nutrition['carbs']
            total_fat += meal_nutrition['fat']
        
        return {
            'calories': round(total_calories, 1),
            'protein': round(total_protein, 1),
            'carbs': round(total_carbs, 1),
            'fat': round(total_fat, 1)
        }

    def clean(self):
        """This ROW's own rules, and only those.

        The overlapping-active-plan check used to live here. It is a CROSS-ROW question,
        which `training_platform/model_validation.py` explicitly forbids in `clean()`,
        and putting it here had two consequences. It ran on EVERY save, including
        `save(update_fields=[...])` long after the plan was committed: request A commits
        and releases the user lock, request B supersedes it and creates its own plan, and
        A's next field update then re-asked the question, saw B's plan, and raised for a
        plan that is already committed and active. And it was a pure check-then-act — two
        requests could both pass it and both insert.

        The rule itself is real and is still enforced, in the one place that can enforce
        it: `DietPersistenceService.save_plan` takes `select_for_update` on the user row
        and supersedes any overlapping active plan before inserting, so the invariant
        holds by serialisation rather than by hope. `overlapping_active_plans()` below
        reports violations for anyone who wants to assert on them.
        """
        from django.core.exceptions import ValidationError

        super().clean()
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({'end_date': 'Must not be before start_date.'})

    def overlapping_active_plans(self):
        """Other active plans of this user's covering any of the same days."""
        if not (self.start_date and self.end_date and self.is_active):
            return DietPlan.objects.none()
        return DietPlan.objects.filter(
            user=self.user, is_active=True,
            start_date__lte=self.end_date, end_date__gte=self.start_date,
        ).exclude(pk=self.pk)

    def save(self, *args, **kwargs):
        self.validate_row()
        super().save(*args, **kwargs)

class Meal(models.Model):
    """
    Represents a meal within a diet plan, including template, date, and description.
    Enhanced with scheduling and completion tracking.
    """
    MEAL_TYPES = [
        ('Breakfast', 'Breakfast'),
        ('Lunch', 'Lunch'),
        ('Dinner', 'Dinner'),
        ('Snack', 'Snack')
    ]
    MEAL_TEMPLATES = [
        ('PROTEIN_CARB', 'Protein + Carb'),
        ('PROTEIN_FAT', 'Protein + Fat'),
        ('CARB_FAT', 'Carb + Fat'),
        ('COMPLETE', 'Complete Meal')
    ]
    template = models.CharField(max_length=20, choices=MEAL_TEMPLATES, default='COMPLETE')
    date = models.DateField(default=date.today)
    diet_plan = models.ForeignKey(DietPlan, on_delete=models.CASCADE, related_name='meals')
    image_url = models.URLField(max_length=500, blank=True, null=True)
    description = models.TextField(blank=True)
    
    # Translations for user-generated content
    translations = models.JSONField(
        default=dict,
        blank=True,
        help_text="JSON translations for dynamic user content (e.g., {'ar': {'description': '...'}})"
    )
    
    is_ai_generated = models.BooleanField(default=False)
    
    # New fields for meal scheduling
    meal_type = models.CharField(max_length=20, choices=MEAL_TYPES, default='Lunch')
    #: What this meal IS. The engine named 79% of meals as real dishes and the name died
    #: at this boundary, because there was nowhere to put it: the client read
    #: "Levantine · about 25 minutes" where the engine had said "Chicken Freekeh Bowl".
    name = models.CharField(max_length=160, blank=True, default='')
    #: Which library dish this is, when it is one. Dish-level variety cannot exist
    #: without it: the recipe window lived only in memory inside one generate() call,
    #: and duration_days defaults to 1, so a daily generator could get the same dish
    #: forever while the food-level no-repeat reshuffled its ingredients.
    recipe = models.ForeignKey('Recipe', null=True, blank=True, on_delete=models.SET_NULL,
                               related_name='meals')
    #: The template shape, when the engine built the meal itself.
    shape = models.CharField(max_length=80, blank=True, default='')
    #: One sentence the engine already knew: "because you chose chicken for lunch".
    reason = models.CharField(max_length=200, blank=True, default='')
    #: Both in Arabic. Composed at generation time from the foods' own translations;
    #: "Mujadara (Quinoa instead of Rice)" cannot be translated after the fact.
    name_ar = models.CharField(max_length=160, blank=True, default='')
    reason_ar = models.CharField(max_length=200, blank=True, default='')
    #: The calories/protein/carb/fat this meal was BUILT to, residual carry included.
    #: The API used to re-derive a target from the plan's split, which is not the
    #: number the planner aimed at, so a correct meal could read as off-target.
    target = models.JSONField(null=True, blank=True)
    scheduled_time = models.TimeField(null=True, blank=True, help_text="Scheduled time for this meal")
    estimated_duration = models.PositiveIntegerField(
        null=True, 
        blank=True, 
        help_text="Estimated duration to complete meal in minutes"
    )
    
    # Meal interaction fields
    is_liked = models.BooleanField(null=True, blank=True, help_text="User's like/dislike rating")
    notes = models.TextField(blank=True, help_text="User's notes about this meal")
    
    class Meta:
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['date', 'scheduled_time', 'id']
        constraints = [
            # `unique_together` was void for 94% of the rows. Postgres treats NULLs as
            # distinct, `scheduled_time` is nullable, and nothing wrote one — so the key
            # never fired and a plan could hold seven breakfasts on one date, which the
            # client saw as seven and could never complete. `nulls_distinct=False`
            # (PostgreSQL 15+) makes two timeless meals in the same slot collide, which
            # is the thing the key was always meant to say. The engine now writes a real
            # time as well (`scheduled_time_for`), so two snacks in a day stay legal and
            # distinguishable — the case that stopped this being a plain
            # (plan, date, meal_type) constraint.
            models.UniqueConstraint(
                fields=['diet_plan', 'date', 'meal_type', 'scheduled_time'],
                nulls_distinct=False,
                name='one_meal_per_slot_per_day'),
        ]
        indexes = [
            models.Index(fields=['is_completed']),
            models.Index(fields=['diet_plan', 'date']),
        ]
    
    def calculate_nutrition(self):
        """
        Calculate total nutrition for this meal.
        """
        components = self.components.all()
        total_calories = 0
        total_protein = 0
        total_carbs = 0
        total_fat = 0
        
        for component in components:
            # Calculate nutrition based on quantity
            scale_factor = component.quantity / component.food.serving_size_grams
            total_calories += component.food.calories * scale_factor
            total_protein += component.food.protein * scale_factor
            total_carbs += component.food.carbs * scale_factor
            total_fat += component.food.fat * scale_factor
        
        return {
            'calories': round(total_calories, 1),
            'protein': round(total_protein, 1),
            'carbs': round(total_carbs, 1),
            'fat': round(total_fat, 1)
        }
    
    # Denormalized completion status for performance
    is_completed = models.BooleanField(default=False, help_text="Whether all components are completed")
    
    def update_completion_status(self):
        """
        Update is_completed status based on components.
        """
        components = self.components.all()
        if not components.exists():
            self.is_completed = False
        else:
            self.is_completed = all(c.is_completed for c in components)
        self.save(update_fields=['is_completed'])
    
    @property
    def completion_percentage(self):
        """Calculate completion percentage of this meal."""
        components = self.components.all()
        if not components.exists():
            return 0
        completed_count = sum(1 for component in components if component.is_completed)
        return round((completed_count / components.count()) * 100, 1)

class MealComponent(models.Model):
    """
    Represents a component (food item) of a meal, with quantity and meal time.
    Enhanced with completion tracking.
    """
    meal = models.ForeignKey('Meal', on_delete=models.CASCADE, related_name='components')
    #: PROTECT, not CASCADE. This row is the client's record of what they ate, and
    #: cascading meant that deleting or retiring ONE catalogue food silently emptied
    #: every historical meal that used it — and `load_food_catalogue --wipe`, which
    #: the release plan runs, deletes every food and therefore every component ever
    #: recorded. This database holds 2,720 meals and zero components for exactly that
    #: reason. The recipe library was already protected; the client's own history was
    #: not. A food that has been eaten is retired with `needs_review`, never deleted.
    food = models.ForeignKey(FoodItem, on_delete=models.PROTECT)
    quantity = models.FloatField(help_text="Quantity in grams")
    meal_time = models.CharField(max_length=20, choices=Meal.MEAL_TYPES, default='Lunch')
    
    # Completion tracking
    is_completed = models.BooleanField(default=False, help_text="Whether this component was consumed")
    completed_at = models.DateTimeField(null=True, blank=True, help_text="When this component was completed")
    actual_quantity_consumed = models.FloatField(
        null=True, 
        blank=True, 
        help_text="Actual quantity consumed (may differ from planned quantity)"
    )
    #: Per-food rating. `Meal.is_liked` credits every food in a liked meal equally, so
    #: a client who disliked one food penalised the other three.
    is_liked = models.BooleanField(null=True, blank=True)
    
    def complete(self, actual_quantity=None):
        """
        Mark this component as completed.
        """
        from django.utils import timezone
        self.is_completed = True
        self.completed_at = timezone.now()
        if actual_quantity is not None:
            self.actual_quantity_consumed = actual_quantity
        self.save()
        
        # Update parent meal status
        try:
            self.meal.update_completion_status()
        except Exception:
            # Optional side effect: swallowing this silently is what made the
            # surrounding failures invisible in logs. Control flow is unchanged.
            logger.debug('suppressed non-fatal error', exc_info=True)

        # And the day's totals, which count components now rather than whole meals — so
        # ticking one ingredient has to move the number the client is looking at. The
        # progress row is CREATED here if it does not exist yet: it used to appear only
        # when the client opened their progress screen, so the first thing they ate
        # before opening it changed nothing they could see.
        try:
            DailyProgress.objects.get_or_create(
                user=self.meal.diet_plan.user, diet_plan=self.meal.diet_plan,
                date=self.meal.date)[0].update_progress()
        except Exception:
            logger.exception("could not recompute progress for component %s", self.pk)

    def calculate_nutrition(self):
        """
        Calculate nutrition for this component.

        The PLANNED amount, which is what this component is worth on the plate. What the
        client actually ate is `actual_quantity_consumed` and belongs to `DailyProgress`.
        """
        scale_factor = self.quantity / self.food.serving_size_grams
        return {
            'calories': round(self.food.calories * scale_factor, 1),
            'protein': round(self.food.protein * scale_factor, 1),
            'carbs': round(self.food.carbs * scale_factor, 1),
            'fat': round(self.food.fat * scale_factor, 1)
        }
    
    class Meta:
        # Deterministic total order. Without it Postgres returns rows in whatever order it
        # likes and LIMIT/OFFSET paging silently repeats and hides rows between pages.
        ordering = ['-id']
        # No `Index(fields=['meal'])` / `Index(fields=['food'])`: a ForeignKey already
        # gets `db_index=True`, so declaring them here built a SECOND btree on each
        # column. This is the hottest insert path in the app — a week-long plan
        # bulk-creates over a hundred rows — and it was paying twice for every index
        # entry, with no read benefit at all.
        # The planner never serves a food twice in one meal, and `converge._write_back`
        # collapses duplicates into one gram figure if it ever sees them. Make the
        # database say so, rather than two code paths each half-guarding it.
        constraints = [
            models.UniqueConstraint(fields=['meal', 'food'], name='one_row_per_food_per_meal'),
            # Grams are a positive amount of food. Nothing checked, and `DailyProgress`
            # sums `quantity / serving_size_grams * calories` straight into what the
            # client reads: a -50 g row persisted -82.5 kcal consumed and a negative
            # completion percentage. The serving size on the other side of that division
            # is already constrained; the numerator was not.
            models.CheckConstraint(condition=models.Q(quantity__gt=0),
                                   name='a_portion_is_a_positive_amount'),
        ]

class DailyProgress(models.Model):
    """
    Tracks daily progress for diet plans.
    """
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE)
    diet_plan = models.ForeignKey(DietPlan, on_delete=models.CASCADE)
    date = models.DateField()
    
    # Progress metrics
    meals_completed = models.PositiveIntegerField(default=0)
    total_meals = models.PositiveIntegerField(default=0)
    calories_consumed = models.FloatField(default=0)
    protein_consumed = models.FloatField(default=0)
    carbs_consumed = models.FloatField(default=0)
    fat_consumed = models.FloatField(default=0)
    
    # Target values
    target_calories = models.FloatField(default=0)
    target_protein = models.FloatField(default=0)
    target_carbs = models.FloatField(default=0)
    target_fat = models.FloatField(default=0)
    
    # Completion status
    is_day_completed = models.BooleanField(default=False)
    completed_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        unique_together = ['user', 'diet_plan', 'date']
        # `id` tiebreaker: a single non-unique sort column is not a total order, so LIMIT/OFFSET
        # paging repeated and hid rows whenever two records shared the value.
        ordering = ['-date', '-id']
    
    @property
    def completion_percentage(self):
        """Calculate overall completion percentage for the day."""
        if self.total_meals == 0:
            return 0
        return round((self.meals_completed / self.total_meals) * 100, 1)
    
    @property
    def calories_percentage(self):
        """Calculate calories percentage of target."""
        if self.target_calories == 0:
            return 0
        return round((self.calories_consumed / self.target_calories) * 100, 1)
    
    @property
    def protein_percentage(self):
        """Calculate protein percentage of target."""
        if self.target_protein == 0:
            return 0
        return round((self.protein_consumed / self.target_protein) * 100, 1)
    
    @property
    def carbs_percentage(self):
        """Calculate carbs percentage of target."""
        if self.target_carbs == 0:
            return 0
        return round((self.carbs_consumed / self.target_carbs) * 100, 1)
    
    @property
    def fat_percentage(self):
        """Calculate fat percentage of target."""
        if self.target_fat == 0:
            return 0
        return round((self.fat_consumed / self.target_fat) * 100, 1)
    
    def update_progress(self, meals_qs=None):
        """
        Update progress based on completed meals.
        Uses aggregation to avoid N+1 queries.
        
        Args:
            meals_qs: Optional queryset of meals to reuse existing data if available.
        """
        from django.db.models import Count, F, FloatField, Q, Sum
        from django.db.models.functions import Coalesce
        
        # Use provided queryset or fetch efficient queryset
        if meals_qs is None:
            meals_qs = self.diet_plan.meals.filter(date=self.date)
            
        # 1. Update basic meal counters (single query)
        counts = meals_qs.aggregate(
            total=Count('id'),
            completed=Count('id', filter=Q(is_completed=True))
        )
        self.total_meals = counts['total'] or 0
        self.meals_completed = counts['completed'] or 0

        # Targets follow the plan. They were written once, into `get_or_create`'s
        # defaults, and never again — so editing a plan's calories or macros left every
        # existing day comparing consumption against a target the plan no longer has,
        # and the percentages the client reads were computed from it.
        plan = self.diet_plan
        macros = plan.macro_targets()
        self.target_calories = float(plan.daily_calories or 0)
        self.target_protein = macros.get('protein', 0)
        self.target_carbs = macros.get('carbs', 0)
        self.target_fat = macros.get('fat', 0)
        
        # 2. What the client actually ate, aggregated in the database.
        #
        # Per COMPONENT, not per meal. The filter was `meal__is_completed=True`, so a
        # client ticking ingredients off as they ate saw 0 kcal all the way through the
        # meal and then the whole meal at once — including the parts they never marked.
        # `EnhancedClientProgressView` showed `completed_components: 2` beside
        # `calories_consumed: 0` in the same response. `complete_entire_meal` writes every
        # component individually, so counting components covers that path too.
        #
        # And the AMOUNT is what they say they ate. `actual_quantity_consumed` is the one
        # field the client fills in to say "I only had a third of it"; it was collected,
        # echoed back over the API, and read by nothing, so a gram of a hundred-gram
        # portion still counted as a hundred grams.
        eaten = Coalesce(
            F('actual_quantity_consumed'), F('quantity'), output_field=FloatField())
        per_gram = eaten / F('food__serving_size_grams')
        stats = MealComponent.objects.filter(
            meal__diet_plan=self.diet_plan,
            meal__date=self.date,
        ).filter(
            Q(is_completed=True) | Q(meal__is_completed=True)
        ).aggregate(
            total_calories=Sum(per_gram * F('food__calories')),
            total_protein=Sum(per_gram * F('food__protein')),
            total_carbs=Sum(per_gram * F('food__carbs')),
            total_fat=Sum(per_gram * F('food__fat'))
        )
        
        self.calories_consumed = round(stats['total_calories'] or 0, 1)
        self.protein_consumed = round(stats['total_protein'] or 0, 1)
        self.carbs_consumed = round(stats['total_carbs'] or 0, 1)
        self.fat_consumed = round(stats['total_fat'] or 0, 1)
        
        # Check completion status
        self.is_day_completed = (self.total_meals > 0 and self.meals_completed == self.total_meals)
        
        if self.is_day_completed and not self.completed_at:
            from django.utils import timezone
            self.completed_at = timezone.now()
        elif not self.is_day_completed:
            self.completed_at = None
            
        self.save()

class DailyAdvice(models.Model):
    """
    Stores AI-generated daily dietary advice for a user.
    """
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE,
                             related_name='daily_advice')
    text = models.TextField()
    generated_at = models.DateTimeField(auto_now_add=True)
    #: The day this advice is FOR, in the client's own calendar. `generated_at` is when
    #: the worker happened to run, which is not the same question and cannot carry a
    #: uniqueness key.
    for_date = models.DateField(null=True, blank=True, db_index=True)
    context_data = models.JSONField()

    class Meta:
        # Deterministic total order. Without it Postgres returns rows in whatever order it
        # likes and LIMIT/OFFSET paging silently repeats and hides rows between pages.
        ordering = ['-id']
        constraints = [
            # One piece of advice per client per day. `CELERY_TASK_ACKS_LATE` and
            # `CELERY_TASK_REJECT_ON_WORKER_LOST` are both on, so losing a worker
            # redelivers the whole sweep — and with a bare `create()` every client got a
            # second row, and a third. Nothing deduplicated them and the endpoint returns
            # the latest, so the duplicates were invisible while the table grew.
            models.UniqueConstraint(fields=["user", "for_date"],
                                    name="one_advice_per_client_per_day"),
        ]


class DietConfig(models.Model):
    """Admin-configurable diet generation settings (piece weights, keywords)."""
    piece_weights = models.JSONField(default=dict, blank=True)
    breakfast_allowed_keywords = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"DietConfig(updated_at={self.updated_at})"

    class Meta:
        # Deterministic total order. Without it Postgres returns rows in whatever order it
        # likes and LIMIT/OFFSET paging silently repeats and hides rows between pages.
        ordering = ['-id']

class Recipe(models.Model):
    """A real dish, so a meal is food rather than a pile of macros.

    The planner selected individual FoodItems and emitted them as a "meal": chicken
    180 g, oats 90 g, olive oil 12 g, broccoli 400 g. That hits the numbers and is not
    something anyone cooks. A recipe carries the composition, so the same targets can be
    met with something a person recognises — and it is also the only way an allergen
    check can know that a dish contains peanuts when its name does not say so.
    """

    MEAL_TYPES = [
        ('Breakfast', 'Breakfast'), ('Lunch', 'Lunch'),
        ('Dinner', 'Dinner'), ('Snack', 'Snack'),
    ]

    name = models.CharField(max_length=160, unique=True)
    description = models.TextField(blank=True, default='')
    meal_types = models.JSONField(
        default=list, blank=True,
        help_text="Which meals this dish suits, e.g. ['Lunch','Dinner']. Empty = any.",
    )
    cuisine = models.CharField(max_length=60, blank=True, default='')
    prep_minutes = models.PositiveIntegerField(default=15)
    instructions = models.TextField(blank=True, default='')
    image_url = models.URLField(blank=True, default='')
    is_active = models.BooleanField(default=True, db_index=True)
    #: Whether a human has checked this dish's gram amounts against a real kitchen.
    #: The library was written to a macro target, not to a plate: it is plausible and
    #: unverified, and a dish nobody has cooked is the one thing tests cannot catch.
    #: The admin filters on this so the review can be done a few dishes at a time.
    reviewed = models.BooleanField(
        default=False, db_index=True,
        help_text="A person has checked these gram amounts against a real plate.")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    def nutrition(self) -> dict:
        """What one serving of this dish, as written, actually comes to.

        The reviewer needs this next to the grams: a number is only wrong relative to
        the plate it produces, and 180 g of rice is right or absurd depending on what
        else is on it.
        """
        totals = {"calories": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0}
        for line in self.ingredients.all():
            food, grams = line.food, float(line.grams or 0)
            if food is None:
                continue
            k = grams / 100.0
            totals["calories"] += float(food.calories or 0) * k
            totals["protein"] += float(food.protein or 0) * k
            totals["carbs"] += float(food.carbs or 0) * k
            totals["fat"] += float(food.fat or 0) * k
        return {key: round(value, 1) for key, value in totals.items()}

    class Meta:
        # `name_en`: see FoodItem.Meta — ordering by the translated `name` made recipe
        # sampling locale-dependent.
        ordering = ['name_en', 'id']
        # No `Index(fields=['is_active'])`: the field already declares `db_index=True`,
        # so this built the same btree twice.

    def __str__(self):
        return self.name

    def allergen_tags(self):
        """Union of every ingredient's allergens — composition, not the dish name."""
        tags = set()
        for line in self.ingredients.select_related('food'):
            tags.update(getattr(line.food, 'allergens', None) or [])
        return sorted(tags)


class RecipeIngredient(models.Model):
    """One food in a recipe, in grams for a single serving."""

    recipe = models.ForeignKey(Recipe, on_delete=models.CASCADE, related_name='ingredients')
    food = models.ForeignKey(FoodItem, on_delete=models.PROTECT, related_name='recipe_lines')
    grams = models.FloatField(help_text="Grams per serving before scaling")
    # A dish scales, but not uniformly: rice flexes, a teaspoon of oil does not.
    scalable = models.BooleanField(
        default=True,
        help_text="False pins the amount when the recipe is scaled to a macro target.",
    )
    note = models.CharField(max_length=120, blank=True, default='')
    #: An author's explicit substitution class for this line — "lean_protein",
    #: "cooked_grain" — so a dish can say "any lean protein" and honour a food the client
    #: chose inside a named dish. Empty means the similarity module decides.
    swap_group = models.CharField(max_length=40, blank=True, default='', db_index=True)

    class Meta:
        ordering = ['-grams', 'id']
        unique_together = [('recipe', 'food')]

    def __str__(self):
        return f"{self.recipe_id}: {self.food.name} {self.grams:g}g"

