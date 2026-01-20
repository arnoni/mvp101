from enum import Enum

class TierStatus(str, Enum):
    FREE = "FREE"
    PAID = "PAID"

class EntitlementService:
    """
    Separates session handling from subscription logic.
    """
    
    @staticmethod
    def check_access(entitlement_key: str) -> TierStatus:
        """
        Checks the entitlement status for a given key.
        
        Args:
            entitlement_key: The unique key derived from the session/user.
            
        Returns:
            TierStatus: FREE or PAID.
        """
        # Stub implementation as per plan
        # In a real implementation, this would check a database or verify a signature
        if not entitlement_key:
            return TierStatus.FREE
            
        # Mock logic: if key starts with "paid_", it's PAID
        if entitlement_key.startswith("paid_"):
            return TierStatus.PAID
            
        return TierStatus.FREE
