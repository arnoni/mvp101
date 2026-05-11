"""Compatibility re-export for the shared PostHog analytics helpers."""

from app.services.analytics import capture, capture_with_properties

__all__ = ["capture", "capture_with_properties"]
