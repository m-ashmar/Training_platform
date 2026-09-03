"""One enforcement point for the rules models declare about themselves.

Twelve models wrote their invariants into `clean()` and nothing ever ran them: DRF
does not call a model's `clean()`, and neither did `save()`. Wiring `full_clean()`
into `save()` closed that hole and opened a worse one. `full_clean()` validates the
*whole* row against *today's* rules, so two kinds of rule that had no business being
there brought the rest down with them:

* rules about another row's present state — a routine required its assigned client's
  *current* trainer to still be the routine's creator, so reassigning a client made
  every routine ever written for them permanently unsaveable;
* rules the stored data had never satisfied, because nothing had ever checked it.

Together those made 1347 existing rows impossible to save, including on writes that
touched none of the fields in question.

The split this module draws:

* **Row-local rules** — decided by this row's own columns — stay in `clean()` and run
  on every write through `validate_row()`. They cannot rot, because nothing outside
  the row can change the answer.
* **Contextual rules** — trainer/client assignment, exercise accessibility, anything
  needing to know *who is acting* — move to the serializer or service that performs
  the action, where the actor is known and the check happens once, at the moment it
  is true.

`validate_row()` runs neither of Django's database re-checks. `validate_constraints()`
and `validate_unique()` each issue a query to test what the database is about to test
itself, and the answer can change between the two — so they cost a query and still race.

Skipping the unique check is not only cheaper, it is required for correctness.
`get_or_create` resolves a lost race by catching the `IntegrityError` the insert raises
and re-fetching the winner's row. A `validate_unique()` that fires first raises
`ValidationError` instead, which `get_or_create` does not catch, so the losing caller
gets an exception rather than the row that now exists. That is how a quota check under
eight-way concurrency denied half the callers who were entitled to pass.

API writes still report a duplicate as a 400: DRF builds `UniqueTogetherValidator` from
the model's own `unique_together`, so the serializer layer checks it where a 400 is the
right answer, and the database has the last word everywhere else.
"""


class RowValidationMixin:
    """Adds `validate_row()` to a model. See the module docstring for the contract."""

    #: Fields left out of row validation, as a tuple of field names. Use it for a
    #: foreign key whose `limit_choices_to` restates a rule `clean()` already reports
    #: with a usable message — Django renders that filter's failure as "instance with
    #: id N does not exist", which sends readers hunting for a row that is right there.
    row_validation_exclude = ()

    def validate_row(self):
        """Run this row's own rules. Raises `django.core.exceptions.ValidationError`."""
        self.full_clean(
            exclude=set(self.row_validation_exclude) or None,
            validate_unique=False,
            validate_constraints=False,
        )
