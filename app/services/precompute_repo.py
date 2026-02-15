from typing import List, Optional, Any
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy import text
from app.models.dto import PrecomputeCandidate
import json

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
                    raw_list = row[0] if isinstance(row[0], list) else json.loads(row[0])
                    # Parse into Pydantic models
                    candidates = []
                    for item in raw_list:
                        try:
                            # Assuming item has lat, lon, category, etc.
                            candidates.append(PrecomputeCandidate(**item))
                        except Exception:
                            continue
                    return candidates
        except Exception:
            # loose fail
            return []
            
        return []
