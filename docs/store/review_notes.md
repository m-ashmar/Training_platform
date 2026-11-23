# Review notes: App Store and Google Play

For the owner, at each submission (spec §9 B8 and §10). Two parts: **§1–§2 paste-ready
notes** for App Store Connect ("App Review Information > Notes") and Play Console ("App
access" and the review notes), then **§3–§10 the facts behind them**, each true of the
code named beside it. Plain and complete on purpose: spec §10 — "never describe the app
as less than it is. An undocumented feature is a violation on its own."

Written 2026-09-27 against `spec/payments-compliance`. `tests/test_store_docs.py` holds
the build table (§4) to `policy/defaults.py` and the account table (§3) to
`seed_review_accounts`, so a change there fails until this file follows.

Marks: **[APP]** the Flutter app session confirms it on the submitted build;
**[OWNER]** a decision or a value only the owner has. Never write a password in this
file or in the repository: it goes in the store's password field only.

## 1. App Store (iOS build) — paste into "Notes"

Replace every `<...>`; keep it under 4,000 characters. **[APP]** the screen names in
the steps are the design's; the app session confirms them on the submitted build.

```
Yalla Gym is a fitness app for Arabic speakers: training programs, workout logging,
nutrition plans, an AI coach, and one-to-one coaching by human personal trainers,
with a chat between each client and their trainer.

This iOS build contains no purchasing and no calls to purchase. It shows no prices,
no buy or subscribe buttons, and no links or instructions to pay anywhere else. A
plan's status and end date, and the wallet's balance and history, are shown for
information only. Whatever a person has, works the same on every platform.

Demo accounts (password in the password field; the same for both):
- Client: <client address>. Has an active Premium plan, a trainer, a 4-day program
  with two weeks of logged workouts, a 7-day meal plan and a running challenge.
- Trainer: <trainer address>. Verified; coaches three clients; has one pending
  request.
Both are in Arabic; <how to switch the app to English, from the app session>.

Things to try (about 10 minutes):
1. Client: Today > open the workout, log a set. Nutrition > today's meals.
2. Client: AI coach > the consent sheet appears first (the coach is powered by an
   outside AI service provider; nothing is sent before consent) > ask a question.
3. Client: Coach > chat with the trainer (text, photo, voice note). Sign in as the
   trainer to answer.
4. Challenges > the running challenge > leaderboard. Report the challenge, or open a
   participant's profile to report or block them; a blocked person disappears from
   the leaderboard, feed, comments and profiles, and neither can message the other.
5. Sign-up: a date of birth under 14 years is refused.
6. Me > Privacy and data > Delete my account (permanent). Please try it on an account
   you create, not on the demo accounts.

The coaching is a one-to-one service between a client and a human trainer, delivered
in real time through the chat and the programs the trainer writes. It cannot be paid
for in this build.

User-generated content: every post, comment, challenge, profile and chat message can
be reported; any person can be blocked; reports go to a moderation queue our team
answers within <target>; a banned-terms filter refuses posts, comments, challenges,
names and usernames containing listed terms.

Privacy policy: <https://.../legal/privacy/?lang=en>
Contact: <contact address>, <phone>
```

## 2. Google Play (Play build) — "App access" and review notes

App access: "All or some functionality is restricted" > add two instructions, "Client"
and "Trainer", each with its address and the password, and the text below.

```
Yalla Gym is a fitness app for Arabic speakers: training programs, workout logging,
nutrition plans, an AI coach, and one-to-one coaching by human personal trainers with
a chat between each client and their trainer.

In the Play build, subscription plans and trainers' ready-made programs are not sold:
no prices, no buy buttons, no links or instructions to pay elsewhere. A plan's status
and end date are shown for information. A trainer's monthly coaching fee is shown and
paid from the in-app wallet: it is a payment from a client to the human trainer who
coaches them, one to one, and the wallet holds cash the client handed to an
authorised agent in person. In this build the wallet pays trainers only; it never
buys app features.

Demo accounts:
- Client: <client address>. Active Premium plan, a trainer, a 4-day program with two
  weeks of logged workouts, a 7-day meal plan, wallet history, a running challenge.
- Trainer: <trainer address>. Verified; three clients; one pending request.

Things to try: log a workout; open today's meals; the AI coach (a consent sheet first);
chat between client and trainer; report or block a participant of the challenge;
account deletion in Me > Privacy and data (please use an account you create).
Web page for account deletion: <https://.../legal/delete-account/?lang=en>
Contact: <contact address>
```

## 3. The demo accounts

Made by `python manage.py seed_review_accounts` (`users/management/commands/
seed_review_accounts.py`), on the production database, after `bootstrap_platform`:

```
REVIEW_ACCOUNTS_EMAIL='review+{role}@<a domain the owner reads>' \
REVIEW_ACCOUNTS_PASSWORD='<strong, from the password manager>' \
python manage.py seed_review_accounts
python manage.py seed_review_accounts --list     # the addresses, nothing changed
```

The password is read from the environment only — never an argument, never in a file.
Run it again shortly before each submission. A run soon after the last one changes
nothing. A later one keeps the accounts alive (ROAD_TO_10 R5):
- it extends the Premium grant when fewer than 30 days are left;
- it makes a new 7-day meal plan when none covers today;
- it moves each programme's end date on when fewer than 14 days are left, since a
  programme past its dates reads `finished` and offers nothing to start;
- when a coached client's month has run out, or ends within 3 days, and their wallet
  cannot cover the next one, the review agent tops the wallet up and the month is
  renewed, so the coaching is paid, not frozen. These are the ordinary agent top-up and
  monthly renewal; the agent stays suspended.

On the single server (`deploy/`) the command runs as `DEPLOY_CHECKLIST.md` section 12
shows, with the password read at a hidden prompt. `{role}` is replaced by the role; without it the address
is plus-addressed (`review+client@...`). A new password rotates it and signs out every
session taken with the old one.

| Role | What the reviewer finds |
|---|---|
| `client` | Samer Haddad (invented). Premium plan granted by an operator for 90 days (no payment row: no money moved). Coached by `trainer`: month 1 approved, paid from the wallet, paid out when the program was assigned. The trainer's 4-day strength program; two weeks of sessions and set logs (one day skipped). A 7-day meal plan. Wallet: an agent top-up and the coaching month. Joined the trainer's "100 sets in two weeks" challenge |
| `trainer` | Rami Mansour (invented). Verified by an operator decision on placeholder images that say they are not identity documents. Three approved clients (`client`, `client2`, `client3`), one pending request (`prospect`), a template and each client's own routine. Marked "not accepting new clients", so no real person can ask this demo trainer for coaching |
| `agent` | The agent who made the top-ups. Not on the public agents list. It has no password and its profile is suspended after those top-ups (every run re-applies both): its address follows from the two given to the reviewers, and an agent who can sign in mints wallet money (SIDE_BUGS S-235) |
| `client2` | Laila Issa (invented), a coached client who started this week |
| `client3` | Karim Youssef (invented), a coached client who started this week |
| `prospect` | Maya Khalil (invented), whose request is waiting for the trainer |

Every name is invented and every phone number is the model's placeholder. Give the
reviewers `client` and `trainer`, and no other account. If Google asks how top-ups
work, describe them (§4); nobody can sign in to `agent` (an agent cannot top up from
the iOS app either, §4).

Nothing on the server treats these accounts differently (rule zero): the group that
marks them is read by the seed command alone, and a test holds every other file to it
(`tests/test_bootstrap_commands.py::test_nothing_but_the_seed_reads_the_review_group`).

Signing in: the app's sign-in screen, address and password (`POST /api/auth/login/`).
No code is asked: the seed creates the accounts the way an operator does, with the
address already confirmed. Five wrong passwords lock the account for 15 minutes.

After a reviewer deletes a demo account, run the seed again: it makes a new one with
the same address.

## 4. What each build shows and sells

The server decides, per build, from the `X-App-Channel` header compiled into it
(`apk`, `play`, `ios`, `web`) and nothing else: the same flags for every user,
reviewer or not, from `GET /api/v1/config/capabilities` (`policy/services.py
flags_for`, which takes a channel and a version and no user). The defaults below are
spec §4's table (`policy/defaults.py`); the console can edit a channel's row, except
that the four store-locked flags can never be switched on for `play` or `ios`
(`policy/models.py STORE_LOCKED_FLAGS`, refused on save and masked on read). **[OWNER]**
check the rows in `/dj-admin/policy/channelcapabilities/` match this table before
submitting.

| Flag | apk | play | ios | web |
|---|---|---|---|---|
| `can_purchase_platform_sub` | yes | no | no | yes |
| `can_purchase_routine_product` | yes | no | no | yes |
| `can_pay_coaching_in_app` | yes | yes | no | yes |
| `can_show_prices` | yes | no | no | yes |
| `can_show_coaching_prices` | yes | yes | no | yes |
| `can_show_web_hub_link` | yes | no | no | no |
| `can_view_entitlements` | yes | yes | yes | yes |
| `can_view_wallet` | yes | yes | yes | yes |
| `can_transfer_wallet` | yes | yes | no | yes |

What that means, and why (spec §3):

| Kind of money | What it is | Store rule | apk | play | ios |
|---|---|---|---|---|---|
| `PLATFORM_SUB` | Basic, Premium, Professional plans (nutrition, the AI coach) | A digital feature: Apple requires in-app purchase, Google requires Play billing | sold | not sold | not sold |
| `ROUTINE_PRODUCT` | A trainer's ready-made program sold to many buyers | A digital good on both stores | sold | not sold | not sold |
| `COACHING_1TO1` | A trainer's monthly fee for coaching one client | A person-to-person service (Apple 3.1.3(d); Google: peer-to-peer payments must not use Play billing) | paid from the wallet | paid from the wallet | not in the first iOS build |
| `WALLET_MOVE` | Agent cash top-ups, client-to-trainer transfers | Real money between people, not a digital good | yes | yes | balance and history only |

"Not sold" is enforced by the server, not only by the app: a plan or program checkout
from `play` or `ios` answers 403 `not_available_on_channel` before anything is read
(`policy/services.py require_purchase_allowed`), and so does asking or accepting a
trainer who charges from a build where coaching is off, a wallet transfer from `ios`,
and an agent's top-up made from the `ios` app. Plan prices are not sent to a build that may not show
them. On `play` and `ios` the AI coach answers any question about paying, prices or
subscribing with one fixed sentence ("I'm the AI coach, so I can't help with payments,
prices or subscriptions...") without calling the model (`ai_assistant/store_safe.py`).
Notices about plans on those builds state the fact ("your plan ends in 3 days") with no
price, button or link.

What a store build still shows: a plan's status and end date (what a person has works
on every build, wherever it was paid), the wallet's balance and history, and on `play`
a trainer's monthly fee.

**[APP]** the store builds carry no price string, no hub URL and no payment instruction
for plans or programs (spec §11: "a search of the iOS and Play builds finds no price
strings, no hub URL and no payment instructions"). The server side is tested; the
binary is the app session's check.

## 5. Guideline 3.1.3(d): one-to-one coaching on iOS

Not in the first iOS build: `can_pay_coaching_in_app` is off, so an iOS client cannot
ask a trainer who charges or accept one's invitation (403 `not_available_on_channel`);
a trainer who charges nothing can be asked from any build. A later, separately
reviewed build enables it by a console row for `ios` with a higher `min_version`
(spec §3), and its notes add:

```
This version lets a client pay their personal trainer's monthly coaching fee from
the in-app wallet. Coaching is a real-time person-to-person service between one
client and one human trainer: the trainer writes the client's own program, reviews
their logged workouts and form-check videos, and talks with them in a two-way chat
with text, photos, voice notes and video. Under guideline 3.1.3(d) (person-to-person
services, which lists fitness training), we collect this payment outside in-app
purchase. Plans and ready-made programs sold to many people are still not sold in
the iOS app.
```

Facts behind it: coaching is sold by the month (`users/coaching.py`); month 1 is held
from the client's wallet when the trainer accepts and paid out when the trainer assigns
the client's program; each renewal moves the same price on the server; the platform
keeps 20% of every month (`COACHING_COMMISSION_RATE`). The chat is two-way and live
over a WebSocket (`ws/chat/{id}/`, `chat/`), with photos, voice notes and videos
(`POST /api/v1/chat/media/`); a client's video naming an exercise waits in the
trainer's form-check queue. Every routine a trainer writes is one client's own copy
(`routine/assignment.py`).

**[OWNER]** the commission means the platform earns from each coaching payment. Apple's
rule is about the service being one-to-one and real-time, which it is; Google's
peer-to-peer wording is less settled for a platform that takes a share. Re-read both
policies before each submission (spec §1).

## 6. The chat, reporting, blocking and moderation (Apple 1.2; Google UGC policy)

| What | How | Where |
|---|---|---|
| Two-way chat | One thread per trainer and client they coach; text, photo, voice note, video; live over a WebSocket, readable for ever, sending stops while the coaching is frozen or ended | `chat/` |
| Report | Anyone signed in can report a person, a post, a comment, a trainer-chat message, an AI coach message, a routine, a diet plan, an exercise or a challenge, with a reason from a fixed list and an optional note | `POST /api/v1/reports/` |
| Block | Symmetric and immediate: neither person sees the other in the feed, comments, follower lists, notifications, profiles, search, trainer lists and matches, the challenge leaderboard; neither can message or send a coaching request to the other | `POST /api/v1/blocks/`, `moderation/services.py blocked_ids` |
| Moderation queue | Operators see every report in the console, oldest first, and close it as "action taken" or "dismissed"; the reporter sees the outcome (`GET /api/v1/reports/mine/`). A suspended account leaves every public surface | `/dj-admin/moderation/report/` |
| Banned-terms filter | Posts, comments, challenges, first and last names, trainer bios and usernames are refused (400 `content_not_allowed`) when they contain a listed term; about 60 Arabic and English terms seeded, operators edit the list | `moderation/ugc_filter.py`, `/dj-admin/moderation/bannedterm/` |
| Contact | The contact address on every legal page | `PLATFORM_CONTACT_EMAIL` **[OWNER]** set it |

What a block does not do, stated so nobody is told otherwise: it does not end an
approved coaching (a client who blocks their trainer is still on the trainer's client
list until the coaching ends), and it does not hide a challenge (the blocked person
leaves its leaderboard). The public trainer directory takes no sign-in and cannot
filter by blocks.

**[OWNER]** a response-time target for the queue (Apple asks for "timely responses";
the notes above say `<target>`), and the people who staff it. Nothing in the code sets
or measures one.

**[OWNER]** the seed makes no community post. To try reporting a post, the reviewer has
to write one first; the challenge, its leaderboard and the chat are there to report
from.

## 7. The AI coach and consent (Apple 5.1.2(i); spec B2)

- Nothing reaches the AI provider until the person agrees: every AI route answers 403
  `ai_consent_required` (over the socket, an error frame) until `POST
  /api/v1/ai-consent/` records a grant with the policy version shown
  (`moderation.AIConsent`). Withdrawing takes effect on the next message; the AI data
  can then be deleted (`DELETE /api/ai/data/`).
- What is sent after consent: the conversation, and what the coach reads to answer —
  the profile (name, age, gender, height, weight, goals, injury note, activity level,
  the trainer's name), workouts and sets, the routine, the diet plan and meals
  (`data_inventory.md`, recipient `ai_provider`).
- The provider is not named in the app; the coach says it is "powered by an outside AI
  service provider" (the owner, 2026-09-24). **[OWNER]** Apple's reviewer may ask which
  provider; the configured one is Cloudflare Workers AI, running the open model Gemma
  (the owner, 2026-11-17).
- "Help us improve" (keeping anonymous copies of conversations to improve the coach) is
  a separate switch, OFF until the person turns it on; the copies carry no name or
  account and are deleted when it goes off, with the account, or after at most 365
  days (`POST /api/ai/training-consent/`).
- Every AI reply carries a medical disclaimer ("The AI coach can make mistakes. It is
  not a substitute for your trainer or your doctor."), and nutrition onboarding and
  every generated plan carry another (Apple 1.4.1; `training_platform/disclaimers.py`).
  Diet plans are built by the platform's own rules, not by the AI; for a person who
  declared an allergy, a dish whose allergen tags are incomplete is left out.

## 8. Account deletion (Apple 5.1.1(v); Google's account deletion requirement)

- In the app: Me > Privacy and data > Delete my account **[APP]**, which calls `DELETE
  /api/privacy/erase/?confirm=ERASE` after showing what will go (`GET`).
- On the web, without the app: `/legal/delete-account/` (the URL is in the capabilities
  answer and goes on the Play listing). It takes an address and an optional reason for
  an operator, who confirms with the owner of the address, erases the account and closes
  the request (`/dj-admin/legal/accountdeletionrequest/`). It answers the same whether or
  not an account uses the address.
- What goes and what stays is in `data_inventory.md`: personal data is deleted or
  anonymised; payment and wallet records are kept without the person's name.
- A trainer who still coaches someone is refused (409 `trainer_has_active_clients`)
  until each coaching ends (the owner, 2026-09-19: a paid month must be delivered). A
  client is never refused for that reason. **[OWNER]** a reviewer who tries to delete
  the demo trainer meets this refusal; the notes ask them to use an account they
  create.
- A copy of everything: `GET /api/privacy/export/` **[APP]** "Download my data".

## 9. Age

Date of birth is required at sign-up and anyone under 14 is refused (`under_minimum_age`,
`MINIMUM_AGE_YEARS = 14`, the owner's decision; spec B5 recommended 16). Nothing else
changes by age. **[OWNER]** the stores' age-rating questionnaires: the app has
user-generated content, a chat between a client and a trainer, and health data; answer
them truthfully, and set Google's target audience to 14 and over (not "children", so the
Families policy does not apply).

## 10. For the owner before each submission

1. `bootstrap_platform` has run on production, then `seed_review_accounts` (§3), within
   a few days of submitting. `--list` shows the six accounts.
2. The capability rows match §4.
3. The approved privacy policy and terms are published in both languages
   (`docs/legal/README.md`), and `PUBLIC_PAGES_BASE_URL`, `PLATFORM_CONTACT_EMAIL`,
   `PLATFORM_NAME` and `PLATFORM_LEGAL_NAME` are set. The store listing's privacy URL is
   `https://<PUBLIC_PAGES_BASE_URL>/legal/privacy/?lang=ar` (and `?lang=en`).
4. `AI_API_KEY` is set (with `AI_BASE_URL` and `AI_ASSISTANT_MODEL`), so the AI coach
   answers the reviewer.
5. The App Privacy and Data safety answers are `privacy_labels.md`.
6. Publisher identity (spec §10): submit under the registered company, state truthfully
   where the service operates (Syria), and expect questions; take legal advice on the
   entity first.
7. Re-read the App Review Guidelines (3.1, 3.1.3(d), 1.2, 5.1.1, 5.1.2) and Google's
   Payments and User Data policies (spec §1).
