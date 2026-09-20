# Design foundation

The rules every screen follows. Written before the first feature so the app
has one voice from the start.

## Who it is for

A client who trains and eats to a plan, on an Android or iPhone that is often
mid-range, on a network that is often poor, in Arabic as often as English.
Trainers use the same app with a different shell. "Premium" here means the
app is fast, calm, legible in both scripts, and never makes the user wait or
guess. It does not mean decoration.

## Principles

1. **One day at a time.** The home is *Today*: the session, the plate, the
   coach. Everything else is one tap away, nothing else is on the home.
2. **No typing during a workout.** Sets are logged with a thumb: steppers,
   dials, a rest ring. The keyboard appears only for search and notes.
3. **Skeletons, never spinners.** A screen renders its shape instantly from
   cache and fills in. A spinner is allowed only inside a button.
4. **Arabic is a first language, not a translation.** One typeface for both
   scripts, RTL-native layout (`EdgeInsetsDirectional`, `AlignmentDirectional`),
   numerals, phones, codes and timers pinned LTR with the `Ltr` widget.
5. **Motion explains, it does not entertain.** Four durations, two curves.
   Reduce-motion is honoured. Haptics have four intents: tap, tick, success,
   error.
6. **Dead ends are bugs.** A control that does nothing yet is not shown, or is
   shown as an honest "Coming soon" block, never as a working button.

## The signature: temperature

The whole app shares one accent, **saffron** (`#F2C14E`), and one surface
system. What changes is temperature: training screens sit on a cool steel wash,
nutrition screens on a warm ember wash, everything else on neutral ink or
paper. The shell animates the wash as you change tab. It is quiet, it is
consistent, and no fitness app in the region does it.

## Colour

| Token | Dark | Light | Use |
|---|---|---|---|
| `bg` | ink0 `#0B0D10` | paper0 `#F6F5F2` | page |
| `surface` | ink1 `#12151A` | paper1 `#FFFFFF` | cards, bars |
| `surfaceRaised` | ink2 `#1A1E25` | paper1 | inputs, rows |
| `surfaceHigh` | ink3 `#242932` | paper2 `#EEECE7` | chips, pressed |
| `outline` | ink4 `#303640` | paper3 `#E3E0DA` | hairlines |
| `text` / `textMuted` / `textFaint` | `#F3F4F6` / `#9AA3B2` / `#5C6673` | `#15181D` / `#5E6672` / `#9AA1AB` | copy |
| `accent` / `onAccent` | saffron / `#1B1400` | same | fills |
| `accentText` | saffron | `#7A5A0E` | accent as text (AA in both modes) |
| `trainTint` / `eatTint` | `#141C27` / `#221A15` | `#EAF0F7` / `#F8EFE6` | temperature |
| `success` / `warning` / `danger` / `info` | `#3DDC97` / `#FF9F1C` / `#FF5C5C` / `#5DA9FF` | darker inks in light | state |
| `protein` / `carbs` / `fat` | `#5B8CFF` / `#E9C46A` / `#FF7A59` | same | macro rings, everywhere |

Widgets read `context.colors`; the raw palette is theme-only.

## Type

**IBM Plex Sans Arabic**, weights 300–700, for both scripts. Arabic gets 10%
more leading and a gentler display tracking; the scale is otherwise shared.
Big numbers use `numeralLarge/Medium/Small` with tabular figures so columns of
weights and macros line up. Eyebrows are small, tracked labels, not small caps.

## Space, shape, motion

- 4-pt grid (`Space.xs … x6`), 20-pt gutters.
- Radii: 8 chips, 14 inputs and buttons, 20 cards, 28 sheets, pill for tabs.
- Durations: 120 fast, 200 base, 320 slow, 480 deliberate. Curves: emphasized
  `cubic(0.2, 0, 0, 1)` for things that move, `easeOutCubic` for fades.
- Buttons are 56 high; touch targets never under 48.

## Screens that exist now

| Screen | What it proves |
|---|---|
| Welcome | brand mark, display type, temperature gradient, one-tap language flip |
| Sign in / Create account | labelled fields, LTR inputs inside RTL, inline `field_errors` |
| Verify | six-box code, autofill, resend cooldown, one generic failure message |
| Suspended | the `account_disabled` dead end handled with dignity |
| Shell + Today | tab wash, time-aware greeting, honest placeholders |
| Me | language and appearance switches, sign out |

## Next design work

Signature interactions for the two core loops, in this order:
1. **Set logging** — stepper + rest ring + haptics, zero typing.
2. **Plate** — meal completion with macro rings and swipe-to-complete.
Then the trainer shell.
