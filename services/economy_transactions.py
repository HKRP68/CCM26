"""Transaction-scoped wallet locks shared by purchases and their reversals.

Call before validation or mutation, then commit/rollback before awaiting any
network operation. Failures deliberately propagate so callers roll back.
"""

from sqlalchemy import update

from models import User


def lock_user_for_update(session, user):
    """Refresh and lock the wallet until the caller ends its transaction.

    PostgreSQL uses a row lock. SQLite ignores FOR UPDATE, so acquire its
    writer lock before reading instead. A process-local click guard alone
    cannot protect requests handled by different workers or clients.
    """
    session.flush()
    if session.get_bind().dialect.name == "sqlite":
        session.execute(
            update(User)
            .where(User.id == user.id)
            .values(total_coins=User.total_coins, updated_at=User.updated_at)
            .execution_options(synchronize_session=False)
        )
    locked = (session.query(User)
              .filter(User.id == user.id)
              .populate_existing()
              .with_for_update()
              .one())
    return locked


def debit_coins(session, user, amount):
    """Debit only if the database wallet can cover the full nonnegative cost.

    The conditional update is the final affordability check even if an older
    caller loaded this user before another request changed their balance.
    No commit: the charge, roster entry and audit record belong together.
    """
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
        raise ValueError("Coin price must be a nonnegative integer")
    session.flush()
    result = session.execute(
        update(User)
        .where(User.id == user.id, User.total_coins >= amount)
        .values(total_coins=User.total_coins - amount)
        .execution_options(synchronize_session=False)
    )
    session.refresh(user)
    return result.rowcount == 1
