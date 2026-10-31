import argparse
import asyncio
import csv
import logging
import math
from datetime import date
from pathlib import Path

from sqlalchemy import text

from app.main import build_async_engine

BATCH_SIZE = 200
LOGGER = logging.getLogger(__name__)

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
    RETURNING id
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
    if confidence is not None and not (0 <= confidence <= 100):
        raise RowValidationError("confidence must be between 0 and 100")

    return {
        "row_num": row_num,
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


def _iter_csv_rows(csv_path: Path):
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        required = {"name", "lat", "lon"}
        headers = set(reader.fieldnames or [])
        missing = required - headers
        if missing:
            raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing))}")

        for i, row in enumerate(reader, start=2):
            try:
                yield _validate_and_transform_row(row, i)
            except RowValidationError as exc:
                raise ValueError(f"Row {i}: {exc}") from exc


def _validate_duplicate_radius_m(duplicate_radius_m: float) -> float:
    if not math.isfinite(duplicate_radius_m):
        raise ValueError("duplicate-radius-m must be a finite float")
    if not (0.0 <= duplicate_radius_m <= 100.0):
        raise ValueError("duplicate-radius-m must be between 0 and 100 meters")
    return duplicate_radius_m


async def add_pois_from_csv(csv_path: Path, *, dry_run: bool, duplicate_radius_m: float) -> None:
    duplicate_radius_m = _validate_duplicate_radius_m(duplicate_radius_m)
    LOGGER.info("Starting POI import from %s", csv_path)

    rows_seen = 0
    inserted = 0
    skipped_duplicates = 0
    seen_in_csv: set[tuple[float, float, str | None]] = set()
    skipped_audit_path = csv_path.with_name(f"{csv_path.stem}.skipped_duplicates.audit.csv")
    audit_file = skipped_audit_path.open("w", encoding="utf-8", newline="")
    audit_writer = csv.DictWriter(
        audit_file,
        fieldnames=["row_num", "name", "lat", "lon", "source", "reason"],
    )
    audit_writer.writeheader()

    if dry_run:
        for row in _iter_csv_rows(csv_path):
            rows_seen += 1
            key = (row["lat"], row["lon"], row["source"])
            if key in seen_in_csv:
                skipped_duplicates += 1
                audit_writer.writerow(
                    {
                        "row_num": row["row_num"],
                        "name": row["name"],
                        "lat": row["lat"],
                        "lon": row["lon"],
                        "source": row["source"],
                        "reason": "intra_csv_duplicate",
                    }
                )
                continue
            seen_in_csv.add(key)
        audit_file.close()
        if rows_seen == 0:
            raise ValueError("CSV has no data rows")
        LOGGER.info(
            "Dry run complete. attempted=%d inserted=%d skipped_duplicates=%d audit=%s",
            rows_seen,
            0,
            skipped_duplicates,
            skipped_audit_path,
        )
        return

    engine = build_async_engine()
    try:
        async with engine.begin() as conn:
            batch_processed = 0
            for row in _iter_csv_rows(csv_path):
                rows_seen += 1
                batch_processed += 1

                key = (row["lat"], row["lon"], row["source"])
                if key in seen_in_csv:
                    skipped_duplicates += 1
                    audit_writer.writerow(
                        {
                            "row_num": row["row_num"],
                            "name": row["name"],
                            "lat": row["lat"],
                            "lon": row["lon"],
                            "source": row["source"],
                            "reason": "intra_csv_duplicate",
                        }
                    )
                    continue
                seen_in_csv.add(key)

                payload = {k: v for k, v in row.items() if k != "row_num"}
                payload["duplicate_radius_m"] = duplicate_radius_m
                try:
                    result = await conn.execute(INSERT_IF_NOT_EXISTS_SQL, payload)
                    inserted_id = result.scalar_one_or_none()
                except Exception:
                    LOGGER.exception(
                        "DB error at CSV row %d (name=%r, lat=%s, lon=%s)",
                        row["row_num"],
                        row["name"],
                        row["lat"],
                        row["lon"],
                    )
                    raise

                if inserted_id is None:
                    skipped_duplicates += 1
                    audit_writer.writerow(
                        {
                            "row_num": row["row_num"],
                            "name": row["name"],
                            "lat": row["lat"],
                            "lon": row["lon"],
                            "source": row["source"],
                            "reason": "existing_db_duplicate",
                        }
                    )
                else:
                    inserted += 1

                if batch_processed >= BATCH_SIZE:
                    LOGGER.info(
                        "Processed %d rows (inserted=%d, skipped_duplicates=%d)",
                        rows_seen,
                        inserted,
                        skipped_duplicates,
                    )
                    batch_processed = 0

            if rows_seen == 0:
                raise ValueError("CSV has no data rows")
    finally:
        audit_file.close()
        await engine.dispose()

    LOGGER.info(
        "Bulk POI import complete: attempted=%d inserted=%d skipped_duplicates=%d audit=%s",
        rows_seen,
        inserted,
        skipped_duplicates,
        skipped_audit_path,
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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    args = parse_args()
    asyncio.run(
        add_pois_from_csv(
            Path(args.csv),
            dry_run=bool(args.dry_run),
            duplicate_radius_m=float(args.duplicate_radius_m),
        )
    )
