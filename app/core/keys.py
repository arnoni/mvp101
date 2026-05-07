class KeyBuilder:
    @staticmethod
    def quota_rolling60(identity_kind: str, identity_id: str) -> str:
        """identity_kind: 'anon' or 'paid' (paid uses user_id)"""
        return f"dd:quota:{identity_kind}:{identity_id}:rolling60"
    
    @staticmethod
    def quota_rolling24h(identity_kind: str, identity_id: str) -> str:
        return f"dd:quota:{identity_kind}:{identity_id}:rolling24h"
    
    @staticmethod
    def quota_block(identity_kind: str, identity_id: str) -> str:
        return f"dd:quota:block:{identity_kind}:{identity_id}"

    @staticmethod
    def quota_idempotency(quota_key: str, idempotency_key: str) -> str:
        return f"dd:quota:idempotency:{quota_key}:{idempotency_key}"
    
    @staticmethod
    def session(sid: str) -> str:
        return f"dd:session:{sid}"
    
    @staticmethod
    def entitlement_status(user_id: str) -> str:
        return f"dd:policy:entitlement:status:{user_id}"
    
    @staticmethod
    def first_paid_query_day(user_id: str, date_str: str) -> str:
        """date_str: YYYYMMDD format"""
        return f"dd:policy:first_paid_query_day:{user_id}:{date_str}"
    
    @staticmethod
    def demand_daily(cell_id: str, date_str: str) -> str:
        return f"dd:metrics:demand:cell:{cell_id}:{date_str}"
    
    @staticmethod
    def demand_rolling14(cell_id: str) -> str:
        return f"dd:metrics:demand:cell:{cell_id}:rolling14"
    
    @staticmethod
    def demand_spike14(cell_id: str) -> str:
        return f"dd:metrics:demand:cell:{cell_id}:spike14"
    
    @staticmethod
    def reputation_score(user_id: str) -> str:
        return f"dd:security:reputation:paid:{user_id}:score"
    
    @staticmethod
    def anomaly_velocity(identity_kind: str, identity_id: str) -> str:
        return f"dd:security:anomaly:velocity:{identity_kind}:{identity_id}"
    
    @staticmethod
    def anomaly_sweep(identity_kind: str, identity_id: str) -> str:
        return f"dd:security:anomaly:sweep:{identity_kind}:{identity_id}"
    
    @staticmethod
    def anomaly_sweep_trace(identity_kind: str, identity_id: str) -> str:
        return f"dd:security:anomaly:sweep:trace:{identity_kind}:{identity_id}"
    
    @staticmethod
    def magic_rate_limit_email(email_hash: str) -> str:
        return f"dd:quota:magic:email:{email_hash}:rolling60"
    
    @staticmethod
    def magic_rate_limit_ip(ip_hash: str) -> str:
        return f"dd:quota:magic:ip:{ip_hash}:rolling60"

    @staticmethod
    def reputation(ip: str) -> str:
        return f"dd:security:reputation:ip:{ip}"
