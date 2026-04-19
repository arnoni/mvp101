from typing import List
from app.models.dto import PrecomputeCandidate, ReportLine

class ReportRenderer:
    """
    Renders opaque reports from raw candidates using cell order.
    """

    @staticmethod
    def render(
        candidates: List[PrecomputeCandidate], 
        user_lat: float, 
        user_lon: float, 
        limit: int = 5
    ) -> List[ReportLine]:
        """
        Returns opaque ReportLines in source order (no in-cell distance ranking).
        """
        _ = (user_lat, user_lon)
        top_k = candidates[:limit]

        lines = []
        for c in top_k:
            text = f"{c.category} (~50m)"
            lines.append(ReportLine(text=text, category=c.category))

        return lines
