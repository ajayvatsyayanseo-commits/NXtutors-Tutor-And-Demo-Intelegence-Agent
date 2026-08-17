"""Versioned contracts. Every cross-boundary shape in the service lives here."""

from tutor_match_meta.contracts.common import (
    SCHEMA_VERSION,
    DataQuality,
    Dimension,
    Evidence,
    Freshness,
    MissingField,
    Provenance,
    Tracked,
    TuitionMode,
    Urgency,
)
from tutor_match_meta.contracts.inbound import (
    InboundEnvelope,
    InboundKind,
    LeadEventV1,
    ParentSelectionV1,
    WhatsAppTurnV1,
)
from tutor_match_meta.contracts.requirement import (
    BudgetBand,
    LocationRequirement,
    MatchRequirementV1,
)
from tutor_match_meta.contracts.schedule import TimeWindow, Weekday, WeeklySchedule
from tutor_match_meta.contracts.scoring import (
    HardFilterRejection,
    MatchDecisionV1,
    ScoredCandidate,
    ShortlistEntry,
    SkillScore,
    missing_score,
)
from tutor_match_meta.contracts.tutor import (
    FeeBand,
    GeoPoint,
    ReviewAggregate,
    TutorCandidate,
    TutorCapabilities,
)

__all__ = [
    "SCHEMA_VERSION",
    "BudgetBand",
    "DataQuality",
    "Dimension",
    "Evidence",
    "FeeBand",
    "Freshness",
    "GeoPoint",
    "HardFilterRejection",
    "InboundEnvelope",
    "InboundKind",
    "LeadEventV1",
    "LocationRequirement",
    "MatchDecisionV1",
    "MatchRequirementV1",
    "MissingField",
    "ParentSelectionV1",
    "Provenance",
    "ReviewAggregate",
    "ScoredCandidate",
    "ShortlistEntry",
    "SkillScore",
    "TimeWindow",
    "Tracked",
    "TuitionMode",
    "TutorCandidate",
    "TutorCapabilities",
    "Urgency",
    "Weekday",
    "WeeklySchedule",
    "WhatsAppTurnV1",
    "missing_score",
]
