"""cell_poi_percentiles — global percentile lookup for ambient scoring.

Design: SINGLE-ROW (percentile = 99.0 only).
- The algorithm only consumes p99 today.
- Multi-row support would be a trivial future migration.
- Single row means a simpler cache key and no ambiguity at query time.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, PrimaryKeyConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CellPoiPercentile(Base):
    __tablename__ = "cell_poi_percentiles"

    percentile: Mapped[float] = mapped_column(Float, nullable=False)
    """Always 99.0 in single-row design."""

    value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    """POI count at the 99th percentile across all cells."""

    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """Number of cells included in the percentile calculation."""

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )

    __table_args__ = (PrimaryKeyConstraint("percentile", name="pk_cell_poi_percentiles"),)
