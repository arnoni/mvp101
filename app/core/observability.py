import sentry_sdk 
from sentry_sdk.integrations.fastapi import FastApiIntegration 
from sentry_sdk.integrations.starlette import StarletteIntegration 
from sentry_sdk.integrations.logging import LoggingIntegration
import logging

def init_sentry(dsn: str | None, env: str, release: str | None) -> None: 
    """ 
    Initialize Sentry. 
    Note: We do not return a middleware. Sentry SDK hooks into FastAPI automatically 
    via the integrations argument. 
    """ 
    if not dsn: 
        print("Sentry DSN not found, skipping initialization.")
        return 

    print(f"Initializing Sentry with environment: {env}")
    logging.info(f"Initializing Sentry with environment: {env}")
    sentry_logging = LoggingIntegration(
        level=logging.INFO,        # Capture info and above as breadcrumbs
        event_level=logging.ERROR  # Send errors as events
    )

    sentry_sdk.init( 
        dsn=dsn, 
        environment=env, 
        release=release, 
        
        # Integrations setup 
        # StarletteIntegration captures the request lifecycle 
        # FastApiIntegration captures the specific route handling 
        integrations=[ 
            StarletteIntegration(transaction_style="endpoint"), 
            FastApiIntegration(transaction_style="endpoint"), 
            sentry_logging,
        ], 

        # Performance Monitoring 
        # Set to 0.0 if you strictly only want error reporting. 
        # Set to e.g. 0.05 (5%) if you want to see how long requests take. 
        traces_sample_rate=0.0, 
        
        # PII Security 
        send_default_pii=False, 
    ) 
