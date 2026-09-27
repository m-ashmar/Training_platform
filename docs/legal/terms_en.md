# DRAFT — Terms of use (English) — NOT APPROVED

For the owner and a lawyer to review, change and approve. Written 2026-09-27 from what
the code does (each rule below is enforced by the server, named in the notes at the
end of this header). Not legal advice, not published: `/legal/terms/` shows a
placeholder until an approved version is published from the console
(`docs/legal/README.md`).

Before publishing: fill every `[OWNER: ...]` and `[LAWYER: ...]` and delete the
brackets; keep it free of prices, payment links and payment instructions (every build,
the App Store and Google Play builds included, opens this page — which is also why no
amount, limit or rate is written here); paste only what is below the BODY line, where
a blank line starts a paragraph, `## ` a heading and `- ` a list item (one line each).

Where each rule lives: minimum age `users/serializers.py` (`MINIMUM_AGE_YEARS`);
coaching by the month, the hold and the renewal `users/coaching.py`,
`users/trainer_relations.py`; the freeze `users/coaching.py training_refusal`,
`subscription` `is_frozen`; plan reminders `subscription.send_renewal_reminders`; no
automatic renewal of plans (`auto_renew` is read-only); withdrawals `payouts/`; content
rules `moderation/ugc_filter.py`, reports and blocks `moderation/`; challenge review
`social/standing.py`; the AI coach's consent and disclaimer `moderation.AIConsent`,
`training_platform/disclaimers.py`; account deletion `training_platform/privacy/`.

===== BODY: paste everything below this line =====

## About these terms

These terms are an agreement between you and [OWNER: registered legal name of the company] ("we"), which operates Yalla Gym. By creating an account or using the app you accept them. Our privacy policy explains how we handle your data.

## Who can use Yalla Gym

- You must be at least 14 years old. We ask for your date of birth when you sign up and refuse anyone younger. [LAWYER: whether users under 18 need a parent's consent.]
- One account per person, with true details. Keep your password to yourself; you are responsible for what is done with your account.
- Clients and trainers create their own accounts. Agents are accounts we create for shops we authorise.

## What Yalla Gym is

Yalla Gym helps you train and eat well: training programs, workout logging, nutrition plans, an AI coach, a community with challenges, and one-to-one coaching by independent personal trainers, with a chat between each client and their trainer.

## Not medical advice

Yalla Gym gives general fitness and nutrition guidance. It is not medical advice and does not replace a doctor. Check with your doctor before starting a program or changing how you eat, especially if you have a health condition, an injury, a severe allergy or are pregnant. Stop exercising and seek help if you feel pain, dizziness or shortness of breath. Plans are built from what you tell us: keep your injuries and allergies up to date. [LAWYER: limitation of liability for injury.]

## Trainers and coaching

- Trainers are independent professionals, not our employees. A trainer who sends us their identity and qualification documents and passes our review is marked verified; verification is not a guarantee of any result.
- Coaching is one-to-one and sold by the month, at the trainer's monthly fee shown before you agree.
- When a trainer accepts you, the first month is taken from your wallet and held; it is paid to the trainer when they deliver your program. Each following month renews from your wallet at the same fee. You are reminded before a month ends.
- If a month cannot be paid, the coaching is frozen: your plan and your trainer's programs stay readable but cannot be used until it is paid. Nothing is deleted.
- You can leave your trainer at any time. If your trainer has not yet delivered the month's program, the held payment returns to your wallet. [OWNER/LAWYER: what happens to a month already delivered.]
- A trainer cannot end a month a client has paid for; a trainer's decision to stop takes effect at the end of the paid month.
- We keep a commission on every coaching fee, shown to trainers before they set their fee. [OWNER: whether to state the rate here.]
- Trainers must coach safely and within their competence, keep clients' data confidential and use it only to coach them, and never ask a client to pay outside the app for coaching arranged in it. [OWNER: confirm this rule.]

## Subscription plans

- Some features, such as the nutrition plans and the AI coach, need an active plan. Your plan, what it includes and its end date are shown in the app.
- A plan does not renew by itself. You are reminded before it ends. When it ends, its features are paused and nothing you created is deleted; they return when a new period starts.
- A plan you hold works the same on every version of the app.
- [LAWYER: refunds, cancellation and consumer rights for plans.]

## Programs from trainers

A trainer may offer a ready-made program. A program you own is a copy for your personal use; you may not resell or share it. If a purchase is refunded, the copy is withdrawn. [LAWYER: refunds.]

## The wallet

- The wallet holds money you top up in cash at an authorised agent. The agent sees your name, your account number and the amount. Keep the receipt.
- From your wallet you pay your trainer's coaching and can send money to a trainer.
- The wallet is not a bank account and earns no interest. Your balance is in your account's currency.
- Trainers withdraw their earnings to a ShamCash address or in cash at a named agent, once verified. Minimums, daily limits and a short settling period apply, shown in the app. We never ask for your ShamCash password, PIN or code.
- If you want to close your account while money is in your wallet, contact us first to get it back.
- [LAWYER: errors, disputes, reversal of mistaken transfers, and the regulatory status of the wallet.]

## The AI coach

The AI coach is software, powered by an outside AI service provider. It reads your data only after you agree, and you can withdraw at any time. It can make mistakes and is not a substitute for your trainer or your doctor. Do not rely on it for medical decisions.

## Community rules

- Be respectful. Do not post or send anything illegal, hateful, harassing, sexual, violent, deceptive or that infringes someone's rights, and do not share other people's personal data.
- No spam, advertising, or links or contact details to take people off the platform.
- Some words and phrases are refused automatically in posts, comments, challenges, names, bios and usernames.
- You can report any post, comment, challenge, profile or message, and block anyone. Blocking hides each of you from the other in the community and stops messages between you.
- We review reports and may remove content, disqualify a challenge result, or suspend or close an account that breaks these rules. [OWNER: the response time you commit to.]
- Challenge results are reviewed for a period after a challenge ends before a winner is settled; we may disqualify results that are not genuine.

## Your content

You keep the rights to what you post and send. You allow us to store it, show it to the people you share it with and to others as the feature intends (for example a post to the community, a trainer's public profile), and to use it to run the service. Trainers allow their clients to use the programs written for them, and buyers to use programs they bought, for their personal training.

## Your account and your data

You can download a copy of your data and delete your account in the app, at any time. A trainer can delete their account once they no longer coach anyone. Deleting is permanent; records of payments are kept as our privacy policy explains. If you cannot open the app, ask on our website: [OWNER: https://yourdomain/legal/delete-account/?lang=en].

## Suspension and termination

We may suspend or close an account that breaks these terms, the law, or puts other people at risk. A suspended account is removed from every public part of the app. [LAWYER: notice, appeal, and what happens to a wallet balance.]

## Liability

[LAWYER: disclaimers of warranty, limitation of liability, indemnity, as allowed by the applicable law.]

## Changes

We may change these terms. We publish each new version at this address with its date and keep the versions you were shown. [OWNER: how users are told of a significant change.]

## Law and disputes

[LAWYER: governing law, jurisdiction, dispute resolution.]

## Contact

[OWNER: registered legal name], [OWNER: address]. Email: [OWNER: contact email address].
