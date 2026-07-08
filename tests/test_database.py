"""
Smoke test for the database hooks (bare-features/database).

Connects to the Postgres instance from docker-compose (published on
localhost:5432), creates the full schema, and round-trips one AccountState row
through the repository layer.

Override the connection string with the DATABASE_URL env var if needed. From
inside a container on the compose network use host `postgres` instead of
`localhost`.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import AccountState, AsyncDatabaseManager, TokenState  # noqa: E402

DEFAULT_URL = "postgresql+asyncpg://hbot:hummingbot-api@localhost:5432/hummingbot_api"


async def main():
    url = os.environ.get("DATABASE_URL", DEFAULT_URL)
    db = AsyncDatabaseManager(url)

    assert await db.health_check(), f"cannot reach database at {url}"
    print("[1] database reachable")

    await db.create_tables()
    print("[2] schema created (tables + lightweight migrations)")

    async with db.get_session_context() as session:
        state = AccountState(
            account_name="bare_features_test",
            connector_name="binance",
            timestamp=datetime.now(timezone.utc),
            token_states=[TokenState(token="USDT", units=Decimal("100"), price=Decimal("1"),
                                     value=Decimal("100"), available_units=Decimal("100"))],
        )
        session.add(state)

    async with db.get_session_context() as session:
        from sqlalchemy import delete, select
        from sqlalchemy.orm import selectinload
        result = await session.execute(
            select(AccountState)
            .options(selectinload(AccountState.token_states))
            .where(AccountState.account_name == "bare_features_test")
        )
        rows = result.scalars().all()
        assert rows, "row did not round-trip"
        tokens = rows[0].token_states
        assert tokens and tokens[0].value == Decimal("100"), f"token state mismatch: {tokens}"
        print(f"[3] AccountState + TokenState round-tripped (id={rows[0].id})")
        await session.execute(delete(TokenState).where(TokenState.account_state_id == rows[0].id))
        await session.execute(
            delete(AccountState).where(AccountState.account_name == "bare_features_test")
        )
        print("[4] test rows cleaned up")

    await db.close()
    print("\nALL DATABASE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
