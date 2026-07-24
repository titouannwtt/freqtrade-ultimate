from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy import DateTime, Float, Integer, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from freqtrade.persistence.base import ModelBase, SessionType
from freqtrade.util import dt_now


class ProfitHistory(ModelBase):
    """
    Fork-specific: periodic snapshot of the bot's current profit (closed + open unrealized),
    sampled by the live loop every few minutes. Powers time-accurate "drawdown including
    open positions" curves in FreqUI — the closed-profit curve alone cannot show when the
    open book was underwater in the past.
    """

    __tablename__ = "profit_history"
    session: ClassVar[SessionType]

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    # Realized profit of all closed trades at sample time (stake currency).
    profit_closed_abs: Mapped[float] = mapped_column(Float, nullable=False)
    # Unrealized profit of open trades at sample time (fees/funding included).
    profit_open_abs: Mapped[float] = mapped_column(Float, nullable=False)
    open_trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return (
            f"ProfitHistory(timestamp={self.timestamp}, closed={self.profit_closed_abs}, "
            f"open={self.profit_open_abs}, n={self.open_trades})"
        )

    @classmethod
    def record(cls, closed_abs: float, open_abs: float, open_trades: int) -> None:
        cls.session.add(
            cls(
                timestamp=dt_now(),
                profit_closed_abs=closed_abs,
                profit_open_abs=open_abs,
                open_trades=open_trades,
            )
        )
        cls.session.commit()

    @staticmethod
    def _naive_utc(dt: datetime) -> datetime:
        # Column values come back tz-naive (UTC) from sqlite; normalise comparisons.
        return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt

    @classmethod
    def get_since(cls, since: datetime | None = None) -> list["ProfitHistory"]:
        q = select(cls).order_by(cls.timestamp.asc())
        if since is not None:
            q = q.filter(cls.timestamp >= cls._naive_utc(since))
        return list(cls.session.scalars(q).all())

    @classmethod
    def prune_older_than(cls, cutoff: datetime) -> int:
        res = cls.session.execute(
            delete(cls)
            .where(cls.timestamp < cls._naive_utc(cutoff))
            .execution_options(synchronize_session=False)
        )
        cls.session.commit()
        return res.rowcount or 0
