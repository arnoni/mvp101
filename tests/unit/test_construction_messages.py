from datetime import date, timedelta

from app.services.construction_messages import get_construction_completion_message

TODAY = date(2026, 5, 12)


def message(score, completion_date=None):
    return get_construction_completion_message(
        score=score,
        nearest_relevant_poi_completion_date=completion_date,
        now=TODAY,
    )


# Score-band boundary tests
def test_score_under_10_returns_quiet_message():
    assert message(9) == "You found yourself a quiet place. Congratulations."


def test_score_10_returns_very_livable_message():
    assert message(10) == "A very livable place. Congratulations."


def test_score_24_returns_very_livable_message():
    assert message(24) == "A very livable place. Congratulations."


def test_score_25_returns_moderate_message():
    assert message(25) == "There is some noise around, but good soundproof windows should keep it livable."


def test_score_49_returns_moderate_message():
    assert message(49) == "There is some noise around, but good soundproof windows should keep it livable."


def test_score_50_returns_high_message():
    assert message(50) == "Construction activity is clearly nearby. Visit during working hours before deciding."


def test_score_74_returns_high_message():
    assert message(74) == "Construction activity is clearly nearby. Visit during working hours before deciding."


def test_score_75_returns_severe_message():
    assert message(75) == "Heavy construction risk nearby. Do not rely on photos — inspect the place in person."


def test_score_100_returns_severe_message():
    assert message(100) == "Heavy construction risk nearby. Do not rely on photos — inspect the place in person."


def test_missing_score_returns_fallback():
    assert message(None) == "Construction analysis complete."


def test_invalid_score_returns_fallback():
    assert message(101) == "Construction analysis complete."


# POI-aware tests
def test_poi_1_day_uses_tomorrow_message():
    assert "tomorrow" in message(60, TODAY + timedelta(days=1))


def test_poi_2_days_uses_days_message():
    assert "finish in 2 days" in message(60, TODAY + timedelta(days=2))


def test_poi_19_days_uses_days_message():
    assert "finish in 19 days" in message(60, TODAY + timedelta(days=19))


def test_poi_20_days_does_not_override_score_message():
    assert message(60, TODAY + timedelta(days=20)) == message(60)


def test_poi_0_days_does_not_override_score_message():
    assert message(60, TODAY) == message(60)


def test_poi_negative_days_does_not_override():
    assert message(60, TODAY - timedelta(days=1)) == message(60)


def test_poi_none_falls_through_to_score():
    assert message(60, None) == message(60)


# POI takes priority even over a quiet score
def test_poi_overrides_quiet_score():
    msg = get_construction_completion_message(
        score=5,
        nearest_relevant_poi_completion_date=TODAY + timedelta(days=8),
        now=TODAY,
    )
    assert "finish in 8 days" in msg


# Singular/plural wording
def test_days_interpolation_singular_tomorrow():
    msg = get_construction_completion_message(
        score=60,
        nearest_relevant_poi_completion_date=TODAY + timedelta(days=1),
        now=TODAY,
    )
    assert "tomorrow" in msg
    assert "1 days" not in msg


def test_days_interpolation_plural():
    msg = get_construction_completion_message(
        score=60,
        nearest_relevant_poi_completion_date=TODAY + timedelta(days=5),
        now=TODAY,
    )
    assert "5 days" in msg


# Privacy regression tests
def test_completion_message_does_not_include_poi_name():
    msg = message(65, TODAY + timedelta(days=8))
    assert "Chocolate" not in msg
    assert "Corner" not in msg


def test_completion_message_does_not_include_poi_distance():
    msg = message(65, TODAY + timedelta(days=8))
    assert "130" not in msg
    assert "meters" not in msg


def test_completion_message_does_not_include_poi_direction():
    msg = message(65, TODAY + timedelta(days=8))
    assert "north" not in msg
    assert "south" not in msg
    assert "east" not in msg
    assert "west" not in msg


def test_completion_message_does_not_include_poi_count():
    msg = message(65, TODAY + timedelta(days=8))
    assert "POI" not in msg
    assert "sites" not in msg
    assert "found" not in msg


def test_unknown_locale_falls_back_to_english_with_interpolation():
    msg = get_construction_completion_message(
        score=60,
        nearest_relevant_poi_completion_date=TODAY + timedelta(days=6),
        now=TODAY,
        locale="unknown",
    )
    assert "finish in 6 days" in msg
