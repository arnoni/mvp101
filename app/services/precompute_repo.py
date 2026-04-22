import json
import logging
from typing import List

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models.dto import PrecomputeCandidate

logger = logging.getLogger(__name__)

class PrecomputeRepository:
    """
    Fetches precomputed POI candidates for a given cell.
    """
    
    def __init__(self, db_engine: AsyncEngine):
        self.db_engine = db_engine
        
    async def get_candidates(self, cell_id: str) -> List[PrecomputeCandidate]:
        """
        Retrieves the list of candidates for a cell.
        Returns empty list if no data or error.
        """
        if not self.db_engine:
            return []
            
        stmt = text("SELECT data FROM cell_poi_precompute WHERE cell_id = :cell_id")
        
        try:
            async with self.db_engine.connect() as conn:
                result = await conn.execute(stmt, {"cell_id": cell_id})
                row = result.fetchone()
                
                if row and row[0]:
                    # row[0] is the JSONB data column
                    payload = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                    raw_list = payload.get("pois", []) if isinstance(payload, dict) else []
                    # Parse into Pydantic models
                    candidates = []
                    for item in raw_list:
                        try:
                            candidates.append(PrecomputeCandidate(**item))
                        except Exception as exc:
                            logger.warning(
                                "E_PRECOMPUTE_CANDIDATE_PARSE_FAILED skipping malformed precompute candidate",
                                extra={"event_code": "E_PRECOMPUTE_CANDIDATE_PARSE_FAILED", "cell_id": cell_id, "candidate": item},
                                exc_info=True,
                            )
                            continue
                    return candidates
        except Exception:
            logger.error(
                "E_PRECOMPUTE_FETCH_FAILED failed loading precompute candidates; returning empty list",
                extra={"event_code": "E_PRECOMPUTE_FETCH_FAILED", "cell_id": cell_id},
                exc_info=True,
            )
            return []
            
        return []
