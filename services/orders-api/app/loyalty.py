"""Loyalty tiers: returning customers get a discount based on how many orders
they've placed with us. Shown in the storefront header and applied at checkout."""
from dataclasses import dataclass

import asyncpg

from .db import db


@dataclass(frozen=True)
class Tier:
    name: str
    min_orders: int
    discount_pct: int


TIERS = (
    Tier("Gold", 15, 10),
    Tier("Silver", 5, 5),
    Tier("Bronze", 0, 0),
)


async def customer_tier(conn: asyncpg.Connection, email: str) -> Tier:
    row = await db.run(
        conn, "loyalty_lifetime_orders", "fetchrow",
        "SELECT count(*) AS orders, coalesce(sum(total_cents), 0) AS spend "
        "FROM orders WHERE customer_email = $1",
        email.lower())
    for tier in TIERS:
        if row["orders"] >= tier.min_orders:
            return tier
    return TIERS[-1]


def apply_discount(total_cents: int, tier: Tier) -> int:
    return total_cents - (total_cents * tier.discount_pct) // 100
