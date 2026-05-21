import argparse
import asyncio
import csv
from datetime import date
from pathlib import Path

from sqlalchemy import text

from app.main import build_async_engine

BATCH_SIZE = 200

INSERT_IF_NOT_EXISTS_SQL = text(
    """
    INSERT INTO pois (
        name,
        category,
        geom,
        source,
        confidence,
        noise_level,
        expected_time_to_complete,
        activity_status
    )
    SELECT
        :name,
        :category,
        ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
        :source,
        :confidence,
        :noise_level,
        :expected_time_to_complete,
        :activity_status
    WHERE NOT EXISTS (
        SELECT 1
        FROM pois p
        WHERE lower(trim(p.name)) = lower(trim(:name))
          AND coalesce(lower(trim(p.source)), '') = coalesce(lower(trim(:source)), '')
          AND ST_DWithin(
              p.geom,
              ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography,
              :duplicate_radius_m
          )
    )
    """
)


class RowValidationError(ValueError):
    pass


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    trimmed = value.strip()
    return trimmed or None


def _parse_optional_int(value: str | None, *, field: str) -> int | None:
    value = _normalize_optional_text(value)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise RowValidationError(f"{field} must be an integer") from exc


def _parse_optional_date(value: str | None) -> date | None:
    value = _normalize_optional_text(value)
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise RowValidationError("expected_time_to_complete must be YYYY-MM-DD") from exc


def _validate_and_transform_row(row: dict[str, str], row_num: int) -> dict:
    name = _normalize_optional_text(row.get("name"))
    if not name:
        raise RowValidationError("name is required")

    try:
        lat = float((row.get("lat") or "").strip())
        lon = float((row.get("lon") or "").strip())
    except ValueError as exc:
        raise RowValidationError("lat/lon must be valid numbers") from exc

    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise RowValidationError("lat/lon out of range")

    activity_status = _normalize_optional_text(row.get("activity_status"))
    if activity_status is not None:
        allowed = {"pending", "active", "paused", "completed"}
        if activity_status not in allowed:
            raise RowValidationError("activity_status must be one of: pending, active, paused, completed")

    noise_level = _parse_optional_int(row.get("noise_level"), field="noise_level")
    if noise_level is not None and not (0 <= noise_level <= 100):
        raise RowValidationError("noise_level must be between 0 and 100")

    confidence = _parse_optional_int(row.get("confidence"), field="confidence")

    return {
        "name": name,
        "category": _normalize_optional_text(row.get("category")),
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "source": _normalize_optional_text(row.get("source")),
        "confidence": confidence,
        "noise_level": noise_level,
        "expected_time_to_complete": _parse_optional_date(row.get("expected_time_to_complete")),
        "activity_status": activity_status,
    }


def _read_csv_rows(csv_path: Path) -> list[dict]:
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        required = {"name", "lat", "lon"}
        headers = set(reader.fieldnames or [])
        missing = required - headers
        if missing:
            raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing))}")

        transformed = []
        for i, row in enumerate(reader, start=2):
            try:
                transformed.append(_validate_and_transform_row(row, i))
            except RowValidationError as exc:
                raise ValueError(f"Row {i}: {exc}") from exc
        return transformed


async def add_pois_from_csv(csv_path: Path, *, dry_run: bool, duplicate_radius_m: float) -> None:
    rows = _read_csv_rows(csv_path)
    print(f"Loaded {len(rows)} rows from {csv_path}")

    if dry_run:
        print("Dry run complete. No database writes performed.")
        return

    engine = build_async_engine()
    inserted = 0
    skipped_duplicates = 0

    try:
        for start in range(0, len(rows), BATCH_SIZE):
            batch = rows[start : start + BATCH_SIZE]
            async with engine.begin() as conn:
                for row in batch:
                    payload = {**row, "duplicate_radius_m": duplicate_radius_m}
                    result = await conn.execute(INSERT_IF_NOT_EXISTS_SQL, payload)
                    if result.rowcount and int(result.rowcount) > 0:
                        inserted += 1
                    else:
                        skipped_duplicates += 1
            print(
                f"Processed {min(start + BATCH_SIZE, len(rows))}/{len(rows)} rows "
                f"(inserted={inserted}, skipped_duplicates={skipped_duplicates})"
            )
    finally:
        await engine.dispose()

    print(
        "Bulk POI import complete: "
        f"attempted={len(rows)}, inserted={inserted}, skipped_duplicates={skipped_duplicates}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="One-off POI bulk importer with quick duplicate protection.")
    parser.add_argument("--csv", required=True, help="Path to CSV file with POIs.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and parse CSV only; no writes.")
    parser.add_argument(
        "--duplicate-radius-m",
        type=float,
        default=1.0,
        help="Distance threshold (meters) for duplicate detection (default: 1.0).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(
        add_pois_from_csv(
            Path(args.csv),
            dry_run=bool(args.dry_run),
            duplicate_radius_m=float(args.duplicate_radius_m),
        )
    )
