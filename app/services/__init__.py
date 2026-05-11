from app.services.construction_score import construction_score

__all__ = ["SearchService", "POIService", "construction_score"]


def __getattr__(name: str):
    if name == "SearchService":
        from app.services.search_service import SearchService

        return SearchService
    if name == "POIService":
        from app.services.poi_service import POIService

        return POIService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
