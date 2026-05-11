"""cell_poi_stats — POI count per 50m x 50m cell, updated daily.

One row per cell. grid_poi_count is the number of raw POIs whose GPS
snaps to this specific cell (NOT including neighboring cells). The
ambient component uses this to compare local density against the
global distribution (p99) while the tier-weighted component handles
the neighborhood via query-time distance rings.
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, PrimaryKeyConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CellPoiStat(Base):
    __tablename__ = "cell_poi_stats"

    cell_id: Mapped[str] = mapped_column(Text, nullable=False)
    """Lower-left corner of the 50m x 50m cell, formatted as 'lat,lon'."""

    grid_poi_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """Number of raw POIs whose GPS snaps to this cell."""

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )

    __table_args__ = (PrimaryKeyConstraint("cell_id", name="pk_cell_poi_stats"),)
