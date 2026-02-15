# app/services/poi_service.py
from typing import List, Tuple, Dict, Any
import structlog
from sqlalchemy import text, bindparam
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.types import Text
from sqlalchemy.ext.asyncio import AsyncEngine
# PublicPOIResult is removed in MVP102 privacy refactor
from app.utils.haversine import haversine
from app.core.config import settings
from pydantic import validate_call

logger = structlog.get_logger(__name__)

class POIService:
    def __init__(self, engine: AsyncEngine | None):
        self.engine = engine
        self.master_list = []

    # MVP102: This service is largely deprecated in favor of PrecomputeRepository.
    # We keep it compilable for now to avoiding breaking strict dependency injection checks if any remain.
    @validate_call
    async def find_nearest_pois(self, user_lat: float, user_lon: float, max_results: int = 5) -> Tuple[List[Dict[str, Any]], List[str]]:
        logs: List[str] = []
        # Return empty immediately as this path is legacy/unsafe
        logs.append("POIService.find_nearest_pois is deprecated/unsafe in MVP102.")
        return [], logs

    @validate_call
    async def get_pois_by_names(self, names: List[str]) -> List[Dict[str, Any]]:
        # Deprecated
        return []

