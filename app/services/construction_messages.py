from datetime import date

from app.services.i18n import t


def get_construction_completion_message(
    score: int | None,
    nearest_relevant_poi_completion_date: date | None,
    now: date,
    locale: str = "en",
) -> str:
    """
    Returns a localized one-line construction completion message.

    POI argument is the expected_time_to_complete of the nearest relevant POI only.
    No POI identity, address, distance, direction, or count is passed in or used.
    """
    if nearest_relevant_poi_completion_date is not None:
        days = (nearest_relevant_poi_completion_date - now).days
        if days == 1:
            return t("construction.message.poi_ending_tomorrow", locale=locale)
        if 2 <= days <= 19:
            return t("construction.message.poi_ending_days", locale=locale, days=days)

    if score is None or not (0 <= score <= 100):
        return t("construction.message.complete_fallback", locale=locale)

    if score < 10:
        return t("construction.message.quiet", locale=locale)
    if score < 25:
        return t("construction.message.very_livable", locale=locale)
    if score < 50:
        return t("construction.message.moderate", locale=locale)
    if score < 75:
        return t("construction.message.high", locale=locale)
    return t("construction.message.severe", locale=locale)
