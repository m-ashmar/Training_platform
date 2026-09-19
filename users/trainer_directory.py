"""Trainers as the v2 app shows them: one card shape, and the matcher that ranks them.

The card is the same everywhere (matches, roster, profile), built for a whole page at
once: `proof` (platform-verified numbers, `users/trainer_proof.py`) and `media`
(`mediakit`) in one query each, never per trainer.

Price: `coaching_price` is present only when the calling BUILD may show a coaching fee
(`policy.services.can_show_price(request, COACHING_1TO1)`); otherwise the key is absent,
not null. Nothing on a card depends on who is asking, and the email is never on it.

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
    """Every trainer the app may show: enabled, with a verified address."""
    from .models import CustomUser

    return CustomUser.objects.filter(user_type='trainer', is_active=True, email_verified=True)


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


def trainer_focus(trainer) -> list[str]:
    """The trainer's focus codes: their own choice, else read from their specialties."""
    if trainer.trainer_goal_focus:
        return list(trainer.trainer_goal_focus)
    text = ' '.join(_strings(trainer.trainer_specializations)).lower()
    return [code for code, words in FOCUS_KEYWORDS.items() if any(w in text for w in words)]


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
            card['coaching_price'] = str(t.trainer_hourly_rate or '0.00')
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


def _score(client, trainer, proof, program_days):
    """(score, reasons) for one trainer. Reasons come out in the order they matter."""
    score, reasons = 0, []
    goal = client.training_goal
    if goal and goal in trainer_focus(trainer):
        score += 40
        reasons.append(_reason('goal_match', lambda: gettext(
            'Specialises in your goal: %(goal)s') % {'goal': GOAL_LABELS[goal]}))
    shared = [a for a in (client.injury_areas or [])
              if a in AREA_LABELS and a in (trainer.trainer_injury_focus or [])]
    if shared:
        score += 20
        reasons.append(_reason('injury_experience', lambda: gettext(
            'Experience with %(area)s injuries') % {'area': AREA_LABELS[shared[0]]}))
    place = client.training_place
    if place and place in (trainer.trainer_training_places or []):
        score += 10
        reasons.append(_reason('training_place', lambda: (
            pgettext('matching reason', 'Programs for training at the gym') if place == 'gym'
            else pgettext('matching reason', 'Programs for training at home'))))
    days = client.training_days_per_week
    if days and days in program_days:
        score += 8
        reasons.append(_reason('program_days', lambda: ngettext(
            'Writes %(days)s-day programs', 'Writes %(days)s-day programs', days)
            % {'days': days}))
    if proof['verified']:
        score += 15
    minutes = proof['typical_response_minutes']
    if minutes is not None:
        score += 10 if minutes <= 180 else 5 if minutes <= 720 else 0
        if minutes <= REASON_MAX_RESPONSE_MINUTES:
            hours = max(1, -(-minutes // 60))
            reasons.append(_reason('fast_response', lambda: ngettext(
                'Usually replies within %(hours)s hour',
                'Usually replies within %(hours)s hours', hours) % {'hours': hours}))
    finished = proof['clients_completed_program']
    score += min(10, finished)
    if finished >= REASON_MIN_COMPLETIONS:
        reasons.append(_reason('completed_programs', lambda: ngettext(
            '%(count)s client finished their program',
            '%(count)s clients finished their program', finished) % {'count': finished}))
    years = trainer.trainer_experience_years or 0
    score += min(5, years // 2)
    if years >= REASON_MIN_YEARS:
        reasons.append(_reason('experience', lambda: ngettext(
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
    from routine.models import Routine

    candidates = list(directory_trainers().filter(trainer_is_available=True).order_by('id'))
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

    top = scored[:limit]
    cards = trainer_cards([t for _s, t, _r in top], request, proof=proof)
    return [{'rank': i + 1, 'fit': _fit(i + 1, score), 'reasons': reasons, 'trainer': card}
            for i, ((score, _t, reasons), card) in enumerate(zip(top, cards))]
