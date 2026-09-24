"""Trainers as the v2 app shows them: one card shape, and the matcher that ranks them.

The card is the same everywhere (matches, roster, profile), built for a whole page at
once: `proof` (platform-verified numbers, `users/trainer_proof.py`) and `media`
(`mediakit`) in one query each, never per trainer.

Price: `coaching_price` and `coaching_currency` are present only when the calling BUILD
may show a coaching fee (`policy.services.can_show_price(request, COACHING_1TO1)`);
otherwise the keys are absent, not null. The price is in the READER's currency (a
signed-in client's own; the price of record for nobody signed in, or when the rate
cannot convert it), and `coaching_currency` always names the unit (`CoachingPriceBook`).
That, and nothing else on a card, depends on who is asking; the email is never on it.

Matching reads what the client answered at onboarding (goal, place, days per week,
injury areas) against what the trainer coaches, plus the proof numbers, and explains
each place with up to three reasons, each in both languages:

    {"code": "goal_match", "text": {"ar": "متخصص في هدفك: بناء العضل",
                                    "en": "Specialises in your goal: building muscle"}}

Reason codes never change once shipped: `goal_match`, `injury_experience`,
`training_place`, `program_days`, `fast_response`, `completed_programs`, `experience`.
"""
from __future__ import annotations

from django.utils import translation
from django.utils.translation import gettext, ngettext, pgettext, pgettext_lazy

from mediakit.services import complete_kits, media_map
from mediakit.slots import OwnerType

from .trainer_proof import proof_for

#: What a free-text specialty says about a goal, for trainers who have not picked their
#: focus codes yet. Lower-case substrings, English and Arabic.
FOCUS_KEYWORDS = {
    'build_muscle': ('muscle', 'hypertrophy', 'bodybuilding', 'body building', 'عضل',
                     'كمال أجسام', 'كمال الأجسام', 'تضخيم'),
    'fat_loss': ('fat', 'weight loss', 'cutting', 'slimming', 'دهون', 'تنحيف', 'خسارة الوزن',
                 'إنقاص الوزن'),
    'strength': ('strength', 'powerlifting', 'power lifting', 'قوة'),
    'general_fitness': ('fitness', 'cardio', 'crossfit', 'cross fit', 'conditioning', 'لياقة',
                        'كروس فت', 'كارديو'),
    'nutrition': ('nutrition', 'diet', 'تغذية', 'حمية'),
}

GOAL_LABELS = {
    'build_muscle': pgettext_lazy('matching goal', 'building muscle'),
    'fat_loss': pgettext_lazy('matching goal', 'losing fat'),
    'strength': pgettext_lazy('matching goal', 'strength'),
    'general_fitness': pgettext_lazy('matching goal', 'general fitness'),
}
AREA_LABELS = {
    'shoulder': pgettext_lazy('injury area', 'shoulder'),
    'lower_back': pgettext_lazy('injury area', 'lower back'),
    'knee': pgettext_lazy('injury area', 'knee'),
    'neck': pgettext_lazy('injury area', 'neck'),
    'wrist': pgettext_lazy('injury area', 'wrist'),
    'ankle': pgettext_lazy('injury area', 'ankle'),
    'hip': pgettext_lazy('injury area', 'hip'),
}

#: Below these, a number is not worth saying out loud.
REASON_MIN_COMPLETIONS = 5
REASON_MIN_YEARS = 5
REASON_MAX_RESPONSE_MINUTES = 6 * 60
MAX_REASONS = 3


def directory_trainers():
    """Every trainer the app may show: publicly visible (`users/visibility.py` — never a
    suspended or erased account, the owner's decision 8, 2026-09-24), with a verified
    address."""
    from .visibility import public_people

    return public_people().filter(user_type='trainer', email_verified=True)


def _strings(value) -> list[str]:
    """A trainer-written JSON field as a list of strings, whatever was stored in it.

    `trainer_specializations` and `trainer_certifications` are JSONFields nothing
    validates, so a trainer can store a number or a bare string. One such row must not
    take the roster and every client's matches down with it.
    """
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v not in (None, '')]
    return []


def _focus_codes(goal_focus, specialities) -> list[str]:
    """The rule itself, over two columns: the trainer's own choice, else what their
    specialities say. Taken out of `trainer_focus` so a caller that only needs to COUNT
    matches can ask without instantiating every trainer (`matches_for_goal`)."""
    if goal_focus:
        return list(goal_focus)
    text = ' '.join(_strings(specialities)).lower()
    return [code for code, words in FOCUS_KEYWORDS.items() if any(w in text for w in words)]


def trainer_focus(trainer) -> list[str]:
    """The trainer's focus codes: their own choice, else read from their specialties."""
    return _focus_codes(trainer.trainer_goal_focus, trainer.trainer_specializations)


def matches_for_goal(client) -> int:
    """How many available trainers suit this client's goal.

    The number the first-day home says out loud ("we found you three trainers who suit
    your goal"), so it must agree with the cards under it: the same candidate set
    `match_trainers` scores — available, in the directory, not blocked either way — and
    the same focus rule. Two small queries and no model instantiation: only two columns
    are read, so nothing decrypts an injury list for a number.
    """
    from moderation.services import blocked_ids

    goal = getattr(client, 'training_goal', '') or ''
    if not goal:
        return 0
    rows = (directory_trainers().filter(trainer_is_available=True)
            .exclude(id__in=blocked_ids(client))
            .values_list('trainer_goal_focus', 'trainer_specializations'))
    return sum(1 for goal_focus, specialities in rows
               if goal in _focus_codes(goal_focus, specialities))


class CoachingPriceBook:
    """Coaching prices for ONE reader, for a whole page: every number in the reader's
    currency, with that currency beside it.

    The owner (2026-09-21): every price a person sees is in their own currency. The
    trainer lists showed `trainer_hourly_rate` — a bare number in the TRAINER's
    currency — so a pound client reading a dollar trainer's `100.00` read it as pounds
    and was then charged 1,300,000 (SIDE_BUGS S-041). The charge was always right
    (`take_hold` converts); the display was not.

    One wallet query for all the page's trainers and at most one rate query
    (`wallet.exchange.price_list_converter`), never a lookup per row. A price that
    cannot be converted (no rate, a stale one) is shown as its price of record in ITS
    currency — the unit is always beside the number, so it cannot be misread. Nobody
    signed in: the price of record. `shown` is False on a build that may not show a
    coaching fee (`policy.services.can_show_price`, `ios` today, or no channel at all):
    every price AND its currency are then None — the keys stay (additive), no price
    string reaches a store build (BACKEND_BUGS B-007). The channel is read once per
    request, not per row.
    """

    def __init__(self, request, trainer_ids=(), *, shown=None):
        from policy.money import MoneyType
        from policy.services import can_show_price
        from wallet import currency_for
        from wallet.exchange import price_list_converter
        from wallet.models import Wallet, _platform_currency

        if shown is None:
            shown = getattr(request, '_coaching_price_shown', None)
        if shown is None:
            shown = can_show_price(request, MoneyType.COACHING_1TO1)
            if request is not None:
                request._coaching_price_shown = shown
        self.shown = shown
        user = getattr(request, 'user', None)
        self.reader_currency = (currency_for(user) if shown and user is not None
                                and user.is_authenticated else None)
        # Nothing to price on a build that may not show one: no wallet query either.
        ids = {pk for pk in trainer_ids if pk is not None} if shown else set()
        self._base = _platform_currency()
        self._currencies = dict(Wallet.objects.filter(owner_id__in=ids)
                                .values_list('owner_id', 'currency')) if ids else {}
        self._convert = price_list_converter(self.reader_currency)

    def currency_of(self, trainer_id) -> str:
        """The trainer's currency — their wallet's, like `wallet.currency_for`."""
        return self._currencies.get(trainer_id, self._base)

    def price(self, amount, currency):
        """`(Decimal | None, currency | None)`, in the reader's currency when it can be.

        A Decimal, not a string: the legacy lists have always sent these as JSON
        numbers and still do; the v2 card turns it into its string itself."""
        if not self.shown or amount is None:
            return None, None
        return self._convert(amount, currency)

    def trainer_rate(self, trainer):
        """The trainer's current monthly price, for this reader."""
        return self.price(getattr(trainer, 'trainer_hourly_rate', None),
                          self.currency_of(trainer.pk))

    def agreed(self, relation):
        """What this relation agreed to, in its own quoted currency, for this reader.

        Exactly what the charge reads (`users/trainer_relations.py price_of_record`),
        so a row can never show one price and approve another: the quote with its unit,
        or — only for a row from before `agreed_rate` existed — the trainer's rate in
        the trainer's currency. Read here without a query per row.
        """
        if relation.agreed_rate is not None and relation.agreed_currency:
            return self.price(relation.agreed_rate, relation.agreed_currency)
        if relation.agreed_rate is not None:
            return self.price(relation.agreed_rate, self.currency_of(relation.trainer_id))
        return self.price(getattr(relation.trainer, 'trainer_hourly_rate', None) or 0,
                          self.currency_of(relation.trainer_id))


def _absolute(request, url):
    return request.build_absolute_uri(url) if request is not None and url else url


def trainer_cards(trainers, request, *, proof=None) -> list[dict]:
    """Cards for `trainers`, in the order given. Two queries for the whole page."""
    from policy.money import MoneyType
    from policy.services import can_show_price

    trainers = list(trainers)
    ids = [t.pk for t in trainers]
    proof = proof if proof is not None else proof_for(trainers)
    media = media_map(OwnerType.TRAINER, ids, request)
    show_price = can_show_price(request, MoneyType.COACHING_1TO1)
    # In the READER's currency, with the unit beside it (SIDE_BUGS S-041).
    prices = CoachingPriceBook(request, ids, shown=True) if show_price else None
    cards = []
    for t in trainers:
        card = {
            'id': t.pk,
            'first_name': t.first_name,
            'last_name': t.last_name,
            'profile_picture': _absolute(request, t.profile_picture.url) if t.profile_picture else None,
            'specializations': _strings(t.trainer_specializations),
            'focus': trainer_focus(t),
            'experience_years': t.trainer_experience_years,
            'proof': proof[t.pk],
            'media': media[t.pk],
        }
        if show_price:
            amount, unit = prices.price(t.trainer_hourly_rate or 0, prices.currency_of(t.pk))
            card['coaching_price'] = str(amount)
            card['coaching_currency'] = unit
            card['money_type'] = MoneyType.COACHING_1TO1.value
        cards.append(card)
    return cards


#: How many of a trainer's public routines the profile shows ("a few", owner).
PROFILE_PUBLIC_ROUTINES = 6


def trainer_profile(trainer, request) -> dict:
    """The profile screen: the card, plus what only the profile shows."""
    from routine.models import Exercise, RoutineTemplate

    card = trainer_cards([trainer], request)[0]
    # Retired exercises (`is_active=False`) are refused to everyone by
    # `Exercise.can_be_accessed_by`, so they are not advertised here either.
    library = Exercise.objects.filter(created_by=trainer, is_active=True).order_by('id')
    card.update({
        'bio': trainer.trainer_bio or '',
        # The trainer's own claim, shown as such; the platform has not checked it.
        'certifications': _strings(trainer.trainer_certifications),
        'library': {
            'exercise_count': library.count(),
            'preview': [{'id': e.pk, 'name': e.name} for e in library[:3]],
        },
        # The routines the trainer chose to make public (templates, `is_public`), newest
        # first. A client's own routine is private to that client and never listed.
        'public_routines': [
            {'id': t.pk, 'name': t.name, 'goal': t.goal, 'days': t.days}
            for t in RoutineTemplate.objects.filter(created_by=trainer, is_public=True)
            .order_by('-created_at', '-id')[:PROFILE_PUBLIC_ROUTINES]],
    })
    return card


# ------------------------------------------------------------------------ matching
def _in_both_languages(render) -> dict:
    out = {}
    for lang in ('ar', 'en'):
        with translation.override(lang):
            out[lang] = str(render())
    return out


def _reason(code, render):
    return {'code': code, 'text': _in_both_languages(render)}


def _rendered(reasons) -> list[dict]:
    """Turn one trainer's deferred reasons into the shipped `{code, text}` shape.

    Called for the few trainers that make the page, never for every candidate — which
    is the whole point of `_score` handing back callables (SIDE_BUGS S-026).
    """
    return [_reason(code, render) for code, render in reasons]


def _score(client, trainer, proof, program_days):
    """(score, reasons) for one trainer. Reasons come out in the order they matter.

    The reasons are DEFERRED: each is `(code, render)`, and `render` is called later,
    by `_rendered`, only for the trainers that survive the ranking. Building the text
    here rendered every candidate's reasons in Arabic and in English — two
    `translation.override` blocks and a catalogue lookup each, up to seven per trainer
    — and then threw all but the top three away, on `trainers/matches/` and on the
    first-day home (SIDE_BUGS S-026). Scoring itself touches no catalogue.
    """
    score, reasons = 0, []
    goal = client.training_goal
    if goal and goal in trainer_focus(trainer):
        score += 40
        reasons.append(('goal_match', lambda: gettext(
            'Specialises in your goal: %(goal)s') % {'goal': GOAL_LABELS[goal]}))
    shared = [a for a in (client.injury_areas or [])
              if a in AREA_LABELS and a in (trainer.trainer_injury_focus or [])]
    if shared:
        score += 20
        reasons.append(('injury_experience', lambda: gettext(
            'Experience with %(area)s injuries') % {'area': AREA_LABELS[shared[0]]}))
    place = client.training_place
    if place and place in (trainer.trainer_training_places or []):
        score += 10
        reasons.append(('training_place', lambda: (
            pgettext('matching reason', 'Programs for training at the gym') if place == 'gym'
            else pgettext('matching reason', 'Programs for training at home'))))
    days = client.training_days_per_week
    if days and days in program_days:
        score += 8
        reasons.append(('program_days', lambda: ngettext(
            'Writes %(days)s-day programs', 'Writes %(days)s-day programs', days)
            % {'days': days}))
    if proof['verified']:
        score += 15
    minutes = proof['typical_response_minutes']
    if minutes is not None:
        score += 10 if minutes <= 180 else 5 if minutes <= 720 else 0
        if minutes <= REASON_MAX_RESPONSE_MINUTES:
            hours = max(1, -(-minutes // 60))
            reasons.append(('fast_response', lambda: ngettext(
                'Usually replies within %(hours)s hour',
                'Usually replies within %(hours)s hours', hours) % {'hours': hours}))
    finished = proof['clients_completed_program']
    score += min(10, finished)
    if finished >= REASON_MIN_COMPLETIONS:
        reasons.append(('completed_programs', lambda: ngettext(
            '%(count)s client finished their program',
            '%(count)s clients finished their program', finished) % {'count': finished}))
    years = trainer.trainer_experience_years or 0
    score += min(5, years // 2)
    if years >= REASON_MIN_YEARS:
        reasons.append(('experience', lambda: ngettext(
            '%(years)s year of experience', '%(years)s years of experience', years)
            % {'years': years}))
    return score, reasons[:MAX_REASONS]


def _fit(rank, score):
    if rank == 1:
        return 'best'
    return 'strong' if score >= 50 else 'good'


def match_trainers(client, request, *, limit=3) -> list[dict]:
    """The trainers who fit `client` best, ranked, each with up to three reasons.

    Candidates: directory trainers who are accepting clients. Ties break by id, so the
    order is stable. The build guide's rule — a trainer cannot be matched FIRST until
    their media kit is complete — moves the best-ranked complete kit to the top whenever
    any candidate has one.
    """
    from moderation.services import blocked_ids
    from routine.models import Routine

    # Nobody is matched with a person they have blocked, or who has blocked them.
    # Here rather than in `directory_trainers()` because the roster and the trainer
    # detail page are `AllowAny` and have no caller to compute a block list from;
    # matching is the one candidate source that always has a signed-in client.
    candidates = list(directory_trainers()
                      .filter(trainer_is_available=True)
                      .exclude(id__in=blocked_ids(client))
                      .order_by('id'))
    if not candidates:
        return []
    ids = [t.pk for t in candidates]
    proof = proof_for(candidates)
    program_days: dict[int, set] = {}
    for trainer_id, days in (Routine.objects.filter(created_by_id__in=ids)
                             .values_list('created_by_id', 'days').distinct()):
        program_days.setdefault(trainer_id, set()).add(days)

    scored = []
    for t in candidates:
        score, reasons = _score(client, t, proof[t.pk], program_days.get(t.pk, set()))
        scored.append((score, t, reasons))
    scored.sort(key=lambda row: (-row[0], row[1].pk))

    kits = complete_kits(OwnerType.TRAINER, ids)
    if kits and scored[0][1].pk not in kits:
        first = next(i for i, row in enumerate(scored) if row[1].pk in kits)
        scored.insert(0, scored.pop(first))

    # Score first, slice, THEN render: the two-language text is built only for the
    # trainers on the page (SIDE_BUGS S-026).
    top = scored[:limit]
    cards = trainer_cards([t for _s, t, _r in top], request, proof=proof)
    return [{'rank': i + 1, 'fit': _fit(i + 1, score), 'reasons': _rendered(reasons),
             'trainer': card}
            for i, ((score, _t, reasons), card) in enumerate(zip(top, cards))]
