"""Seeded synthetic dataset generator.

Generates a deterministic (fixed seed) dataset of ~300 failed payments over
14 days that resembles real Indian-merchant failure mixes, including the
edge cases the policy engine must handle (opted-out customers, very high
values, already-recovered payments, duplicates).

ALL DATA IS SYNTHETIC AND SIMULATED. Run `python -m app.seed --force` to rebuild.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timedelta, timezone
from typing import List, NamedTuple

from app.config import settings
from app.db import Base, SessionLocal, engine
from app.models import AuditLog, Customer, Payment
from app.razorpay.types import PaymentEntity, RazorpayCustomer
from app.utils import mask_phone

SEED = 42
DAYS = 14
TOTAL_FAILURES = 300

FIRST_NAMES = [
    "Aarav", "Diya", "Vihaan", "Ananya", "Aditya", "Ishita", "Arjun", "Kavya",
    "Rohan", "Meera", "Sai", "Priya", "Karthik", "Lakshmi", "Rahul", "Sneha",
    "Vikram", "Divya", "Nikhil", "Pooja", "Manoj", "Revathi", "Sanjay", "Anjali",
]
# Language mix: English majority, Hindi and Tamil minorities (South-India skew).
LANG_BY_NAME_REGION = {
    "ta": ["Karthik", "Lakshmi", "Revathi", "Manoj", "Sai"],
    "hi": ["Aarav", "Diya", "Vihaan", "Ananya", "Aditya", "Ishita", "Rohan", "Priya"],
    "en": None,  # everyone else
}

# Failure mix (weights chosen to resemble reported merchant distributions:
# UPI timeouts dominate, then bank-side technical errors, then customer-side).
FAILURE_MIX: list[tuple[str, str, str, str, str, float]] = [
    # (reason, code, description, source, step, weight)
    ("timeout",           "GATEWAY_ERROR",     "Payment did not complete within the time limit", "network",   "payment_initiation", 0.30),
    ("bank_technical_error", "GATEWAY_ERROR",  "Bank server is unavailable, try again later",    "bank",      "payment_initiation", 0.18),
    ("insufficient_funds", "BAD_REQUEST_ERROR", "Insufficient funds at the issuing bank",       "bank",      "authorization",      0.20),
    ("authentication_failed", "BAD_REQUEST_ERROR", "Authentication failed: incorrect OTP or 3DS failure", "customer", "authentication", 0.12),
    ("customer_cancelled", "BAD_REQUEST_ERROR", "Payment cancelled by the customer",             "customer",  "payment_initiation", 0.09),
    ("card_blocked_or_fraud", "BAD_REQUEST_ERROR", "Transaction declined: card blocked / suspected fraud", "bank", "authorization",    0.07),
    ("network_error",     "SERVER_ERROR",      "Connection to the bank was interrupted",         "network",   "payment_initiation", 0.04),
]
_TOTAL_WEIGHT = sum(w for *_, w in FAILURE_MIX)


class FailureSpec(NamedTuple):
    reason: str
    code: str
    description: str
    source: str
    step: str


def _pick_failure(rng: random.Random) -> FailureSpec:
    roll = rng.random() * _TOTAL_WEIGHT
    acc = 0.0
    for reason, code, desc, source, step, weight in FAILURE_MIX:
        acc += weight
        if roll <= acc:
            return FailureSpec(reason, code, desc, source, step)
    return FailureSpec(*FAILURE_MIX[0][:5])


def _pick_amount(rng: random.Random) -> int:
    """Amounts in paise. Mostly small-ticket, with a deliberate high-value tail.

    Rs 149 - Rs 8,000 for 90% of payments; Rs 8,000 - Rs 24,000 for 8%;
    > Rs 25,000 (the human-approval threshold) for ~2%.
    """
    roll = rng.random()
    if roll < 0.90:
        rupees = rng.randint(149, 8_000)
    elif roll < 0.98:
        rupees = rng.randint(8_000, 24_000)
    else:
        rupees = rng.randint(26_000, 90_000)  # deliberately above the threshold
    return rupees * 100


def _pick_method(rng: random.Random, reason: str) -> str:
    # UPI dominates Indian checkout; cards still common for higher values.
    if reason in ("card_blocked_or_fraud", "authentication_failed"):
        return rng.choice(["card", "card", "upi"])
    weights = [("upi", 0.62), ("card", 0.24), ("netbanking", 0.09), ("wallet", 0.05)]
    roll = rng.random()
    acc = 0.0
    for method, w in weights:
        acc += w
        if roll <= acc:
            return method
    return "upi"


def _make_customers(rng: random.Random, count: int) -> List[Customer]:
    customers: List[Customer] = []
    used_phones: set[str] = set()
    for i in range(count):
        name = rng.choice(FIRST_NAMES)
        lang = "en"
        for candidate_lang, names in LANG_BY_NAME_REGION.items():
            if names and name in names:
                lang = candidate_lang
                break
        # ~3% opted out (edge case the guardrails must honor).
        opted_out = rng.random() < 0.03
        while True:
            phone = "9" + "".join(rng.choice("0123456789") for _ in range(9))
            if phone not in used_phones:
                used_phones.add(phone)
                break
        customers.append(
            Customer(
                customer_id=f"cust_seed_{i:04d}",
                name=name,
                masked_phone=mask_phone(phone),  # full number never stored
                language=lang,
                opted_out=opted_out,
            )
        )
    # Guarantee the opted-out edge case exists regardless of random draw.
    opted_count = sum(1 for c in customers if c.opted_out)
    for c in customers:
        if opted_count >= 2:
            break
        if not c.opted_out:
            c.opted_out = True
            opted_count += 1
    return customers


def generate_payments() -> List[dict]:
    """Pure generator: returns payment dicts + attached customer index.

    Deterministic given SEED — same output on every machine/run.
    """
    rng = random.Random(SEED)
    customers = _make_customers(rng, 120)

    start = datetime(2026, 9, 10, tzinfo=timezone.utc)
    payments: List[dict] = []

    for i in range(TOTAL_FAILURES):
        day_offset = rng.uniform(0, DAYS)
        # Business-hours skew: fewer failures at night.
        hour = rng.choices(
            range(24), weights=[1, 1, 1, 1, 1, 2, 3, 5, 8, 10, 11, 11, 10, 10, 11, 12, 12, 11, 10, 8, 6, 4, 2, 1]
        )[0]
        ts = int((start + timedelta(days=day_offset)).replace(
            hour=hour, minute=rng.randint(0, 59), second=rng.randint(0, 59)
        ).timestamp())

        cust = rng.choice(customers)
        spec = _pick_failure(rng)
        amount = _pick_amount(rng)
        method = _pick_method(rng, spec.reason)
        attempt_count = rng.choices([1, 2, 3], weights=[0.70, 0.22, 0.08])[0]

        payments.append({
            "payment_id": f"pay_seed_{i:04d}",
            "order_id": f"order_seed_{i:04d}",
            "customer": cust,
            "amount": amount,
            "method": method,
            "spec": spec,
            "ts": ts,
            "attempt_count": attempt_count,
            "edge": None,
        })

    # --- carve out explicit edge cases (deterministic slots) ---
    # 2 opted-out customers: force 2 payments onto opted-out customers.
    opted = [c for c in customers if c.opted_out][:2]
    for j, c in enumerate(opted):
        payments[j]["customer"] = c
        payments[j]["edge"] = "opted_out"

    # 3 very-high-value payments (>= Rs 25,000 -> human approval required).
    for j, amount in enumerate([45_00_000, 62_50_000, 28_00_000]):  # 45k, 62.5k, 28k Rs
        payments[10 + j]["amount"] = amount
        payments[10 + j]["edge"] = "very_high_value"

    # 3 already-recovered payments (customer paid via a later attempt).
    for j in range(3):
        payments[20 + j]["edge"] = "already_recovered"

    # 2 duplicates: same order retried -> same order_id, new payment_id.
    payments[30]["order_id"] = payments[29]["order_id"]
    payments[30]["edge"] = "duplicate"
    payments[40]["order_id"] = payments[39]["order_id"]
    payments[40]["edge"] = "duplicate"

    payments.sort(key=lambda p: p["ts"])
    return payments


def reset_db() -> None:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def run_seed() -> dict:
    reset_db()
    rows = generate_payments()

    # Register payments with the app's simulated gateway (only when simulated
    # so fetch_payment() works in the demo; never touches a real client).
    from app.razorpay.factory import get_razorpay_client
    client = get_razorpay_client()
    from app.razorpay.simulated import SimulatedRazorpayClient as _Sim
    sim = client if isinstance(client, _Sim) else None

    counts = {"payments": 0, "customers": 0, "recovered": 0, "opted_out": 0,
              "high_value": 0, "audit": 0}
    seen_customer_ids: dict[str, Customer] = {}

    with SessionLocal() as db:
        for row in rows:
            cust = row["customer"]
            db_cust = seen_customer_ids.get(cust.customer_id)
            if db_cust is None:
                db.add(cust)
                seen_customer_ids[cust.customer_id] = cust
                counts["customers"] += 1
            db_cust = seen_customer_ids[cust.customer_id]

            status = "open"
            recovered_at = None
            recovered_amount = None
            if row["edge"] == "already_recovered":
                status = "recovered"
                recovered_at = row["ts"] + 3600 * rng_jitter(row["payment_id"])
                recovered_amount = row["amount"]
                counts["recovered"] += 1

            payment = Payment(
                payment_id=row["payment_id"],
                order_id=row["order_id"],
                customer=db_cust,
                amount_paise=row["amount"],
                method=row["method"],
                error_code=row["spec"].code,
                error_description=row["spec"].description,
                error_source=row["spec"].source,
                error_step=row["spec"].step,
                error_reason=row["spec"].reason,
                created_at=row["ts"],
                attempt_count=row["attempt_count"],
                status=status,
                recovered_at=recovered_at,
                recovered_amount_paise=recovered_amount,
                needs_human_review=False,
            )
            db.add(payment)
            counts["payments"] += 1

            if row["edge"] == "opted_out":
                counts["opted_out"] += 1
            if row["amount"] >= settings.high_value_paise:
                counts["high_value"] += 1

            # Register with the simulated gateway so fetch_payment() works.
            if sim is not None:
                sim.register_payment(PaymentEntity(
                    id=row["payment_id"],
                    order_id=row["order_id"],
                    amount=row["amount"],
                    method=row["method"],
                    error_code=row["spec"].code,
                    error_description=row["spec"].description,
                    error_source=row["spec"].source,
                    error_step=row["spec"].step,
                    error_reason=row["spec"].reason,
                    created_at=row["ts"],
                    customer=RazorpayCustomer(name=cust.name),
                ))

        # Seed a single provenance row in the audit log.
        db.add(AuditLog(
            ts=int(datetime.now(tz=timezone.utc).timestamp()),
            payment_id="",
            rule_id="",
            action="seed",
            reason=f"seeded {TOTAL_FAILURES} synthetic failures over {DAYS} days (seed={SEED})",
            message_source="system",
            outcome="seeded",
            detail={"seed": SEED, "total": TOTAL_FAILURES, "days": DAYS},
        ))
        counts["audit"] += 1
        db.commit()

    return counts


def rng_jitter(payment_id: str) -> int:
    """Deterministic small jitter derived from the payment id."""
    return sum(ord(c) for c in payment_id) % 26


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the RecoverAI demo dataset")
    parser.add_argument("--force", action="store_true", help="drop and recreate tables")
    args = parser.parse_args()
    if not args.force:
        print("Refusing to reseed without --force (data would be reset).")
        sys.exit(1)
    counts = run_seed()
    print(
        f"Seeded {counts['payments']} payments / {counts['customers']} customers "
        f"(recovered={counts['recovered']}, opted_out={counts['opted_out']}, "
        f"high_value={counts['high_value']}) into {settings.db_url}"
    )


if __name__ == "__main__":
    main()
