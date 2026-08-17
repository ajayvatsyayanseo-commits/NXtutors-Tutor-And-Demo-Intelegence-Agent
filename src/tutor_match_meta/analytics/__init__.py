"""Sanitised analytics. Pseudonymous dimensions only, never message text."""

from tutor_match_meta.analytics.events import (
    ALLOWED_DIMENSIONS,
    AnalyticsEventName,
    AnalyticsEventV1,
    AnalyticsSink,
    LoggingAnalytics,
    NullAnalytics,
    PostgresAnalytics,
    RecordingAnalytics,
    UnsafeDimension,
    class_band,
    coverage_band,
)

__all__ = [
    "ALLOWED_DIMENSIONS",
    "AnalyticsEventName",
    "AnalyticsEventV1",
    "AnalyticsSink",
    "LoggingAnalytics",
    "NullAnalytics",
    "PostgresAnalytics",
    "RecordingAnalytics",
    "UnsafeDimension",
    "class_band",
    "coverage_band",
]
