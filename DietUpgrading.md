## Diet System Upgrading Log (Phased)

Purpose: Act as an engineering memory and operator’s guide that chronicles all upgrades across phases, why they were made, their impact, risks, and how to work with the new structure. This file is updated and appended after each phase completes with tests green.

---

### Phase 1 — Architecture & Modularity (Core Refactor)

Date: 2025-09-28

#### What changed
- Broke the monolithic `DietGenerator` into a small set of focused services:
  - `diet/services/prompt_builder.py` (PromptBuilder)
    - Builds GPT prompt from a Jinja2 template and structured context.
  - `diet/services/ai_response_handler.py` (AIResponseHandler)
    - Calls OpenAI (Responses/Chat) and parses output using Pydantic models.
  - `diet/services/diet_persistence.py` (DietPersistenceService)
    - Persists generated plans/meals/components; softly enforces user category pools.
- Extracted utilities:
  - `diet/utils/nutrition.py` — grams conversion, piece-food identification, fuzzy match.
  - `diet/utils/http.py` — JSON POST with retry on read-timeout.
- Externalized the giant prompt string into a template:
  - `diet/templates/diet/diet_plan_prompt.jinja2`.
- `diet/ai_services.py` now acts as a facade/orchestrator and delegates to the new services. Generation metadata/flow preserved.

#### Why
- Improve separation of concerns and testability.
- Enable quick iteration on prompts without code changes.
- Allow future provider swaps or parallel model strategies without changing business logic.
- Keep persistence rules (e.g., category alignment) explicit and isolated from prompting/LLM logic.

#### Engineering methods followed
- Single-responsibility services.
- Template-driven prompt assembly.
- Utility-first approach for shared logic.
- Defensive programming around parsing, conversion, and retries.

#### Benefits
- Faster prompt evolution; prompt in a `.jinja2` file.
- Easier unit/integration testing per boundary.
- Ingredient mapping now “softly enforces” per-meal macro pools: tries closest allowed item; if none, keeps original (no data loss).
- Clear lines for future performance improvements or caching.

#### Risks & mitigations
- Risk: divergence between prompt intent and persistence mapping.
  - Mitigation: both services consume `UserFoodCategoryPreference`. We treat mapping as a soft constraint.
- Risk: template rendering errors.
  - Mitigation: Jinja rendering handled via Django engines; failing render is surfaced in logs with context.

---

### Phase 2 — Error Handling & Logging

Date: 2025-10-01

#### Test status
- Diet app tests: 19/19 PASS.

#### What changed (exceptions, retries, logging)
- Custom exception hierarchy (`diet/exceptions.py`):
  - `OpenAIError`, `DietParsingError`, `PersistenceError`, `ConstraintViolationError`, plus `HTTPTransientError` and `HTTPPermanentError` to classify HTTP failures.
- Exponential backoff retries (`tenacity`) in `diet/utils/http.py`:
  - Retries transient errors (timeouts, 429, 5xx) with jitter; permanent 4xx bubble up.
- Structured, PII-safe logging (`diet/utils/logging_utils.py`):
  - `log_json` helper with PII redaction, using `structlog` if available; stdlib fallback otherwise.
- Services wired with exceptions and logging:
  - `AIResponseHandler`: wraps provider/parse errors as `OpenAIError`/`DietParsingError` and logs structured errors.
  - `DietPersistenceService`: uses transactions, maps `IntegrityError/ValidationError` to `ConstraintViolationError`, logs JSON with redaction.
  - `PromptBuilder`: logs template rendering failures and raises `DietError`.
- Celery task behavior (`diet/tasks.py`):
  - Retries only on transient provider/HTTP errors; no-retry on parsing/persistence/validation; logs structured JSON.
- Dependencies: added `tenacity`; ensured `structlog` present.

#### Why
- Clear error taxonomy improves debuggability and recovery behavior.
- Backoff avoids hammering providers and stabilizes integrations.
- PII-safe structured logs enable safe observability and downstream ingestion.

#### Engineering methods followed
- Typed error boundaries, atomic transactions, and explicit transient/permanent classification.
- Idempotent-safe persistence within DB transactions.
- Consistent, structured logging with redaction.

#### Benefits
- Fewer noisy retries and faster surfacing of permanent failures.
- Safer logs for production with consistent shape.
- Easier on-call triage via error categories.

#### Risks & mitigations
- Risk: over-redaction may hide useful signals.
  - Mitigation: maintain allowlist fields and include IDs/metrics; never log raw prompts or PII.
- Risk: misclassification of HTTP codes.
  - Mitigation: conservative mapping; easily adjustable in `utils/http.py`.

---

### Current system overview

- Orchestration: `DietGenerator` (facade) → `PromptBuilder` → `AIResponseHandler` → `DietPersistenceService`.
- Prompting: Jinja2 template `diet/diet_plan_prompt.jinja2` uses rich context including `UserFoodCategoryPreference` to shape meal choices per meal/macro.
- Persistence: categorization-soft enforcement for ingredients; per-meal macro rebalance logic preserved.
- Async: diet plan generation remains Celery-based (Redis broker); worker must be running for async POST `/api/diet/api/generate-plan/`.
- Routes: `/api/diet/...` and `/diet/...` supported (same targets) for compatibility.
- Tests: diet tests all green; future phases should keep this invariant.

---

### Operator notes / DX

- To test generate → nutrition quickly:
  1) Start Celery worker and broker.
  2) POST `/api/diet/api/generate-plan/` with optional `start_date`.
  3) GET `/api/diet/api/client/progress/enhanced/?date=YYYY-MM-DD` to obtain `diet_plan.id`.
  4) GET `/api/diet/api/nutrition/plan/{plan_id}/?date=YYYY-MM-DD`.

- To adjust prompt rules: edit `diet/templates/diet/diet_plan_prompt.jinja2` only.
- To refine ingredient mapping: adjust `DietPersistenceService` and `diet/utils/nutrition.py`.

---

### Next steps (future phases)

- Phase 3 (example):
  - Add caching/layered retries for OpenAI calls; structured logging of token usage.
  - Configurable provider (OpenAI/Groq/self-host) via settings.
  - Add unit tests for services/utilities.

- Phase 4 (example):
  - Feature-flag subscription gating for food endpoints in production.
  - Add analytics hooks for prompt effectiveness and plan adherence.

---

### Changelog summary

- Added: `diet/services/*`, `diet/utils/*`, `diet/templates/diet/diet_plan_prompt.jinja2`.
- Updated: `diet/ai_services.py`, `diet/views.py`, `diet/models.py`, `training_platform/urls.py`.
- Passing tests: 19/19 diet tests.

---

Maintainers: When you complete a new phase (with tests green), append a new section with date, exact changes, rationale, risks, and operator guidance.

### Phase 3 — Database & Persistence

Date: 2025-10-01

#### Test status
- Diet app tests: 19/19 PASS.

#### What changed (DB access optimization and persistence refactor)
- Optimized reads/caching:
  - Cached `UserFoodCategoryPreference` per user during plan save; constructed strict in-memory index.
  - Prefetch-like retrieval of `DietConfig` values once per save via `_load_piece_weights`.
- Persistence refactor:
  - `MealPlanFactory` builds `Meal` and `MealComponent` objects and centralizes quantity conversion.
  - `MealValidator` enforces allergens and optional strict category pools.
  - `MealRebalancer` performs macro balancing post-insert (extracted from generator code path).
  - Replaced fuzzy `SequenceMatcher` with strict dictionary/index mapping; when no match, keep original ingredient (no loss).
- Transactions and safety:
  - Wrapped plan save in a DB transaction; maps `IntegrityError/ValidationError` to `ConstraintViolationError`.

#### Why
- Reduce query count and improve persistence throughput.
- Make persistence stages explicit and independently testable (factory → validator → rebalancer).
- Eliminate fuzzy-matching ambiguity; prefer deterministic mapping.

#### Engineering methods followed
- Transactional writes with clear error mapping.
- Deterministic mapping using prebuilt indices.
- Separation of concerns across factory/validator/rebalancer units.

#### Benefits
- Fewer DB round-trips during plan creation.
- Predictable, auditable ingredient mapping with option to tighten enforcement.
- Easier to evolve macro balancing independently of persistence or prompting.

#### Risks & mitigations
- Risk: strict mapping may miss reasonable near-matches.
  - Mitigation: keep original ingredient if no exact index hit; expose toggles for strict pools.
- Risk: validator may filter too many items if allergies are overly broad.
  - Mitigation: default validator runs non-strict; category strictness can be enabled per environment.


