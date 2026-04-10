from app.api.routes import _map_user_report_to_ugc
from app.schemas.user_reports import UserReportRequest, ReportKind


def test_map_user_report_empty_note_uses_fallback_title_description():
    payload = UserReportRequest(
        lat=16.047079,
        lon=108.206230,
        report_kind=ReportKind.MAYBE_CONSTRUCTION,
        is_nearby_now=True,
        note="",
    )

    mapped = _map_user_report_to_ugc(payload)

    assert mapped.title == "Possible construction observed"
    assert mapped.description == "Possible construction observed"


def test_map_user_report_long_note_keeps_description():
    payload = UserReportRequest(
        lat=16.047079,
        lon=108.206230,
        report_kind=ReportKind.ACTIVE_CONSTRUCTION,
        is_nearby_now=False,
        note="Observed workers and heavy equipment this morning.",
    )

    mapped = _map_user_report_to_ugc(payload)

    assert mapped.title == "Active construction observed"
    assert mapped.description == "Observed workers and heavy equipment this morning."


def test_map_user_report_short_note_keeps_user_text():
    payload = UserReportRequest(
        lat=16.047079,
        lon=108.206230,
        report_kind=ReportKind.MAYBE_CONSTRUCTION,
        is_nearby_now=False,
        note="happy",
    )

    mapped = _map_user_report_to_ugc(payload)

    assert mapped.title == "Possible construction observed"
    assert mapped.description == "happy"
