# RecoverAI — BUILD_LOG

Honest, chronological log of what was built, what broke, root causes, and fixes.
(Per buildathon rules: synthetic data, simulated outcomes; this log reflects the build,
not production operation.)

---

## Phase 1 — Data + Razorpay layer

**Built**
- Project scaffold: `requirements.txt`, `Makefile` (`demo` / `seed` / `test` / `clean`),
  `.gitignore`, `.env.example`, `README.md`.
- `app/config.py` — env-driven settings; zero-config defaults (no LLM key, no Razorpay keys
  needed to run).
- `app/db.py` — SQLite + SQLAlchemy 2.0, WAL journal mode, FK enforcement pragma.
- `app/models.py` — `Customer`, `Payment` (paise ints, Razorpay-style error fields,
  recovery state machine), `Decision`, `AuditLog` (append-only), `IdempotencyKey`.
- `app/clock.py` — simulation clock (thread-safe fast-forward; nothing calls `time.time()`
  directly for business logic).
- `app/utils.py` — PII masking, Indian-grouping INR formatting, IST helpers.
- `app/seed.py` — deterministic generator: 300 failures / 14 days, realistic mix
  (UPI timeout 30%, bank technical 18%, insufficient funds 20%, auth/OTP 12%, cancelled 9%,
  blocked/fraud 7%, network 4%) + edge cases: 2 opted-out, 3 very-high-value (> ₹25k),
  3 already-recovered, 2 duplicate-order retries.
- `app/razorpay/` — `types.py` (Razorpay-shaped dataclasses), `base.py` (interface),
  `simulated.py` (in-memory, deterministic ids, CHAOS hooks), `real.py` (test-mode REST
  client, never raises for upstream errors), `factory.py`.
- `app/webhooks.py` — payload models + HMAC-SHA256 signature verify/derive helpers.
- `app/ingest.py` — replay-safe normalization; masks phones on the way in.
- `app/main.py` — FastAPI app: `POST /webhook/payment_failed` (signature verified over the
  RAW body BEFORE parsing), `GET /api/health`, index placeholder.
- Tests: `tests/test_phase1.py` (utils/clock/clients), `tests/test_webhook_security.py`
  (signature, raw-body binding, replay dedupe), `tests/test_seed.py` (determinism, mix,
  edge cases). **Result: 26 passed.**

**Bugs / wrong turns**

1. **`db.py` written corrupted** — an invalid placeholder line inside `create_engine(...)`
   made the module unimportable.
   *Root cause:* a malformed generation stream, not a logic error.
   *Fix:* rewrote the file cleanly; pragmas moved into a `connect` event listener.

2. **FastAPI 500 at route registration on `GET /`** —
   `FastAPIError: Invalid args for response field! ... FileResponse | JSONResponse`.
   *Root cause:* FastAPI builds a pydantic response model from the return annotation;
   a union of two Response types isn't a valid pydantic field.
   *Fix:* `@app.get("/", response_model=None)`.

3. **`format_inr(123456700)` produced `Rs 1,,23,4,,567.00`** in tests.
   *Root cause:* I formatted with `f"{rupees:,.2f}"` (western grouping already applied),
   then re-grouped Indian-style on top → doubled/misplaced commas.
   *Fix:* format with `{:.2f}` (no grouping), then apply Indian grouping once
   (last 3, then pairs).

4. **`mask_phone("+919876543210")` masked 12 digits** instead of the 10-digit national
   number.
   *Root cause:* no normalization of the `+91` country code / `0` trunk prefix before
   masking, so the same customer could render differently by input format.
   *Fix:* strip `91`/`0` prefixes (12/11-digit forms) before masking.

5. **`NameError: _ts_now` in `models.py`** — the `Payment.updated_at` default referenced
   the helper defined at the bottom of the module.
   *Root cause:* class body executes at import; helper wasn't defined yet.
   *Fix:* moved `_ts_now` above the model classes.

6. **`RealRazorpayClient` would have crashed on first success** — passed a nonexistent
   `link_from_api=` kwarg into the `CreateLinkResult` dataclass (TypeError at runtime,
   uncaught by tests since real client isn't exercised without keys). Also `expire_seconds`
   was silently ignored.
   *Fix:* removed the bogus kwarg; map `expire_seconds` to Razorpay's `expire_by`
   (now + seconds). Noted for Phase 6: add a mocked-HTTP test so this path is covered
   without keys.

7. **Seeder registered simulated payments with a throwaway client** — `run_seed()` created
   its own `SimulatedRazorpayClient`, so the app's factory client (different instance)
   would have answered `payment_not_found` for every seeded payment in Phase 4.
   *Root cause:* two "simulated gateway" instances with separate state.
   *Fix:* register payments on the app's factory-managed client (only when simulated).

8. **Non-deterministic edge-case guarantee** — opted-out customers came from a 3% random
   draw; with 120 customers there was a real chance of < 2 opted-out, breaking the seeded
   edge-case contract (caught while writing `test_seed.py`).
   *Fix:* after generation, force-promote customers until the minimum count exists.

9. **Environment note** — local machine runs Python 3.13.3, not 3.11. All code is written
   3.11-compatible (no 3.12+ syntax); CI/buildathon machine with 3.11 will work unchanged.

---

## Phase 2 — Deterministic policy engine (the brain)

**Built**
- `app/policy/classify.py` — error reason/description/source/step → category
  (`technical | insufficient_funds | customer_action | risk | unknown`). Unknown reasons
  classify conservatively as `unknown` → human review, never guessed as recoverable;
  risk patterns in the description (fraud/blocked) are checked FIRST so a novel code can't
  sneak a fraud case into auto-recovery.
- `app/policy/probability.py` — documented lookup table reason × method → (p_24h, p_7d),
  attempt falloff (p × 0.6^attempts), wildcard fallback per method.
- `app/policy/engine.py` — pure `decide(PolicyInput) -> PolicyDecision` with explicit rule
  IDs: R1 (retry ×2 with 30m/2h backoff), R2 (wait 24h → nudge w/ alternate method),
  R3 (nudge now), R4 (review, never auto-recover; human-approved manual contact possible)
  + guardrails G1 opted-out, G2 max-3-attempts, G3 > ₹25,000 needs approval (approval
  unlocks the normal rule), G4 quiet hours 21:00–08:00 IST, G5 daily message budget.
  Decision shape: {action, rule_id, reason, inputs, probability, expected_recovery_paise,
  eligible_at_ts}. Eligibility for waits/holds uses the simulation clock.
- `app/policy/queue.py` — loads open payments, runs the engine, persists Decision rows,
  sorts by expected recovered ₹ (amount × probability).
- Tests: `tests/test_policy_engine.py` (25 tests) — every rule, every guardrail incl.
  boundary cases (20:59 vs 21:00, exactly-at-threshold amounts), precedence, scoring.
  **Result: 51 passed total.**

**Design decisions (documented)**
- R1 auto-retries are customer-visible (UPI collect pings the phone) so QUIET HOURS apply
  to them; but they are not messages, so the DAILY BUDGET doesn't. R2's 24h wait touches
  nobody, so neither applies to the wait itself.
- G3 approval is sticky (stored `review_cleared_at`) and unlocks the normal category rule.
- R4 + human approval → one manual nudge allowed with a conservative documented estimate
  (p=0.10), still subject to quiet hours/budget/attempts.

**Bugs / wrong turns**

10. **Specific reason codes scored as zero recovery probability** —
    `get_probability("wrong_otp", ...)` returned 0 because the probability table is keyed
    by canonical reasons (`authentication_failed`) while classification emits specific
    Razorpay-style codes. Two vocabularies with no bridge; every auth-failure payment
    would have silently ranked to the bottom of the queue.
    *Root cause:* table and classifier evolved independently.
    *Fix:* `REASON_ALIASES` map (wrong_otp → authentication_failed, etc.) + regression
    test asserting aliased reasons score > 0.

11. **PostgreSQL-only JSON operator in the daily-budget count** — queue counted today's
    messages with `AuditLog.detail["date_ist"].astext == today`, which crashes on SQLite
    (no JSON astext). Caught by inspection, not by tests (budget path not yet
    end-to-end).
    *Fix:* count by timestamp range via new `ist_day_bounds()` helper (IST calendar day).

12. **Phantom attribute + lazy init in queue.py** — referenced `decision.eligible_probability`
    (doesn't exist) behind a `hasattr` guard, and `new_decisions = persist and [] or []`.
    *Fix:* direct attribute, plain list init.

---

## Phase 3 — LLM message writer with validation

**Built**
- `app/llm/gemini.py` — dependency-free Gemini REST client (`generateContent`), 6s timeout,
  raises `GeminiError` on network/HTTP/shape problems. Prompt carries ONLY: first name,
  amount, language, merchant, link, tone — no phones, no PII. Token usage + latency captured.
- `app/llm/validator.py` — the strict gate: exact `Rs X.XX` amount string present (blocks
  changed/rounded amounts), exact sanctioned link present, no foreign URLs, ≤300 chars,
  script check (Devanagari for hi, Tamil for ta, Latin-dominant for en), no
  discount/cashback/refund/waiver promises (EN + Hindi/Tamil keywords), no urgency/threat
  phrasing, no phone numbers, name personalization present. Collects ALL violations, not
  fail-fast, for audit debugging.
- `app/llm/templates.py` — per-language (en/hi/ta) templates with an "alternate methods"
  variant for R2; guaranteed to pass the validator (enforced by test).
- `app/llm/writer.py` — `compose_message()`: LLM → validate → template fallback; records
  {message_source, fallback_used, fallback_reason, latency_ms, tokens, validation_errors}.
  Never raises. With no `GEMINI_API_KEY` the app is fully functional (template path).
- Tests: `tests/test_llm.py` (20) — validator happy paths in all 3 scripts, adversarial
  outputs (changed amount, rounded amount, substituted link, too long, wrong script,
  discount promises ×4, urgency threats ×3, leaked phone), templates-always-valid
  (parametrized ×3 languages ×3 amounts), fallback on LLM error AND on hostile output via
  monkeypatch fault injection, metrics logging. **Result: 71 passed total.**

**Bugs / wrong turns**

13. **Garbled Unicode escapes in the forbidden-word regexes** — the first write of the
    validator's Hindi/Tamil discount/threat patterns came out as invalid Devanagari/Tamil
    escape soup (wrong codepoints, `\u0d2d` = Malayalam in a Hindi pattern, dangling
    string concatenation). Would have silently missed vernacular violations.
    *Root cause:* hand-assembling escape sequences instead of writing the script text.
    *Fix:* rewrote with literal Hindi/Tamil keywords (छूट, रिफंड, कैशबैक, माफ़;
    தள்ளுபடி, ரீஃபண்ட், கேஷ்பேக், சலுகை; threat phrases) + regression tests in each
    category. English patterns carry the main load; vernacular is defense-in-depth.

**Design notes**
- "Fallback" is reserved for *tried-and-failed*; with no key configured the writer returns
  `source=template, fallback_used=false` — the dashboard can distinguish "LLM down" from
  "LLM never configured".
- Templates carry an explicit alternate-methods variant so R2's "offer an alternate
  method" behavior is real, not just prose in the reason string.

---

## Phase 4 — Executor + audit trail (+ a bonus guardrail)

**Built**
- **G6 contact cooldown** (added during executor design): max one customer message per 4h
  per payment. Without it, every agent run would re-nudge R3 customers — idempotency keys
  only prevent duplicates within the same attempt ordinal, not across new ordinals.
  Cooldown applies to messages only; R1 retries keep their own designed backoff.
- R2 wait bookkeeping fixed properly: the engine now receives `last_wait_ts` (derived from
  the audit trail) and distinguishes "wait not started" (decide wait) from "24h in
  progress" (stay waiting) from "elapsed" (nudge). The old `recovery_attempts == 0` proxy
  would have re-waited forever / or nudged instantly depending on interpretation.
- `app/executor.py` — `Executor.run_agent()`: executes queue actions via the
  RazorpayClient; **insert-first idempotency keys** (`payment:action:ordinal`) committed
  BEFORE acting, so crashes/double-clicks can never double-nudge; append-only audit rows
  for every action/hold/skip/error; per-run summary (retries/nudges/waits/holds/ignores/
  reviews/errors, LLM vs template counts, fallbacks).
- **Seeded outcome simulator** (documented, SIMULATED): after each customer-visible action,
  recovery may resolve after a 2h grace window; the roll is
  `random.Random(f"outcome:{payment_id}:{ordinal}")` compared against the engine's own
  24h probability (incl. attempt falloff) — same payment + attempt always yields the same
  result, so demo runs are reproducible. Recovered amount == payment amount (no discounts,
  no partials). Every resolution is audit-logged with `simulated: true` in the detail.
- `app/api.py` — `/api/kpis`, `/api/queue`, `/api/agent/run`, `/api/clock/advance`,
  `/api/clock/reset`, `/api/audit`, `/api/chart_data` (failures by reason + baseline vs
  RecoverAI on the same seeded data), `/api/payments/{id}/message` (sent message or live
  preview, with source + fallback info), `/api/payments/{id}/review` (approve/skip),
  `/api/meta`. Every response carries `simulated: true`.
- Tests: `tests/test_executor.py` (10) — idempotency claims, double-run zero duplicates,
  fresh link + message contents, review gating (flagged items never nudged before
  approval), deterministic outcomes, no double-counting on re-resolve, edge-case honors.
  **Result: 84 passed total.**

**Bugs / wrong turns**

14. **Schema drift: `pending_outcome_at` added to the model after the demo DB existed** —
    `no such column: payments.pending_outcome_at` when smoke-testing against the DB seeded
    before the column was added. Tests were green (they create schema fresh), the demo DB
    wasn't.
    *Root cause:* SQLite + `create_all` never migrates existing tables; no migration tool
    in a buildathon stack.
    *Fix:* `make demo` re-seeds with `--force` (drop + create). Documented; real fix at
    scale = Alembic.

15. **Time-of-day flaky tests** — executor tests ran at whatever the wall clock was; when
    I ran them during IST night hours, G4 quiet hours correctly held every action and the
    suite "failed". The tests were wrong, not the engine.
    *Fix:* `clock.set_offset()` helper; the fixture pins the sim clock to 2026-09-15
    10:00 IST so guardrails behave deterministically; fast-forwards use `advance()`.

16. **Edge cases lost by slicing sorted data** — `generate_payments()[:40]` in the test
    fixture sliced the TIME-SORTED list, but edge cases are attached at pre-sort indices
    (0,1 / 10,11,12 / 20,21,22 / 30,40), so the fixture randomly lost them and
    `assert reviews >= 1` failed.
    *Fix:* use the full 300-row dataset in tests (still deterministic, ~2s).

17. **`in_("retry", "nudge")`** — SQLAlchemy's `in_()` takes a sequence, not varargs;
    would have crashed at first audit query. Caught by inspection pre-run.

18. **Midnight demo trap (design bug found by smoke test)** — ran the flow at ~00:00 IST:
    run 1 held ALL 205 contacts (G4 working as designed), and "Fast-forward 24h" kept the
    same time of day, so a night demo could never show a send. Also: advancing exactly 24h
    resolved 0 outcomes because pending eligibility was still in the future relative to
    quiet-hours holds.
    *Fix:* `/api/clock/advance` with `hours=24` now extends the jump to the next 08:00 IST
    when the target lands inside quiet hours. Re-run after advance correctly executed
    115 nudges + 149 retries with 0 holds.

19. **Windows console `UnicodeEncodeError` (cp1252)** while printing Hindi message
    previews from a smoke script — display-only; the API/DB serve UTF-8 correctly. Used
    `python -X utf8` for console scripts.

---

## Phase 5 — Dashboard

**Built**
- `static/index.html` — single dark-theme page (Tailwind CDN + Chart.js, no build step):
  5 KPI cards (total failed ₹, recovered ₹, recovery rate, at-risk ₹, human-review count),
  failures-by-reason chart, baseline-vs-RecoverAI chart (same seeded data, labeled
  SIMULATED), prioritized queue sorted by expected recovered ₹ (columns: payment, amount,
  reason, action badge, rule ID, expected ₹; Approve/Skip on review rows), message preview
  panel (text + LLM/TEMPLATE badge + fallback reason + latency/tokens), live audit trail,
  Run-agent / Fast-forward-24h / Reset-clock buttons, auto-refresh, header pills
  (SIMULATED DATA / LLM mode / sim clock). A persistent "⚠ SIMULATED DATA" badge and
  footer disclaimer make the demo-honesty constraint impossible to miss.

**Verified in-browser** (Chromium against the live server): KPIs and charts populate,
  Run-agent reports the same counts as the backend, message preview renders Hindi
  templates correctly (UTF-8), Approve decrements the review counter and writes a
  `human_review` audit row, G4 holds visible at night IST.

**Bugs / wrong turns**

20. **Human-review KPI counted already-approved items** — clicked Approve, the count
    stayed at 28 because the KPI summed `needs_human_review` regardless of decision.
    *Root cause:* conflated "ever flagged" with "awaiting decision".
    *Fix:* count only `review_cleared_at IS NULL` rows (27 after the approval).

21. **Editing a large HTML file reliably** — two writes of index.html truncated mid-file
    (transport-level, same as the earlier corrupted files).
    *Fix:* wrote the complete file in a single call and verified by loading the page.

---

## Phase 6 — Reliability + proof (CHAOS)

**Built**
- `CHAOS=1` fault injection (env, wired through `settings.chaos`):
  `SimulatedRazorpayClient` fails ~25% of `create_payment_link` and ~15% of
  `fetch_payment` calls; the LLM path degrades via the writer's catch-all fallback.
- `tests/test_chaos.py` (5 tests): full LLM outage → every nudge still sends with
  `source=template, fallback_used=true`; writer-level catastrophe (non-GeminiError)
  → facade never raises; forced link failures → errors audited, **idempotency keys
  released**, previously-failed payments successfully retried on the healed run;
  random chaos → no crash, summary stays consistent; chaos flag surfaces in /api/health.
- **Idempotency hardening (found by the chaos tests):** an error AFTER the key was
  claimed (link failure, crash, unexpected exception) used to leave the key claimed and
  the attempt ordinal never advanced → the payment was blocked from that nudge FOREVER.
  Fix: `_release_idempotency_key()` on any handled failure; crash-claims remain (by
  design — a crash mid-send must never double-send; operators replay manually).
- **Writer hardening:** `compose_message` now catches ANY exception from the LLM call
  (not just GeminiError) — an SDK bug or code defect can no longer crash a nudge; the
  template floor holds. **Result: 89 passed total.**

**Bugs / wrong turns**

22. **Chaos tests injected faults at the wrong layer** — I first monkeypatched
    `executor.compose_message` to raise; the facade's contract is to never raise, so the
    exception propagated into the executor's generic handler and every nudge errored
    (nudges=0, errors=56). The test modeled the failure in a place it can't happen.
    *Fix:* inject at the real failure point (`write_message` inside the writer) + added an
    explicit test that even a writer-level catastrophe falls back cleanly.

23. **monkeypatch restore captured the patched method** — `setattr(..., SimulatedRazorpayClient.create_payment_link)`
    AFTER patching captured the failing stub (self-reference), so the "healed" run still
    failed and the key-release assertion failed.
    *Fix:* capture `original_link_method` BEFORE patching, restore that.

24. **The two bugs above hid a real one:** the executor's outer error handler audited
    exceptions but never released post-claim idempotency keys — the permanent-block bug
    from #22's discovery applied to retry/nudge handlers generally. Fixed with
    try/except/release in both handlers (see hardening note above).

---

## Final state

- **89 pytest tests passing** across data, webhook security, policy, LLM/validator,
  executor/idempotency, chaos.
- `make demo` → seeded data + dashboard on :8000; `make test` → full suite;
  `make seed` → rebuild deterministic dataset.
- Honest-labeling everywhere: SIMULATED badges in UI, `simulated: true` in API
  responses, documented assumptions in `app/policy/probability.py` and the outcome
  simulator, limitations section in README.

