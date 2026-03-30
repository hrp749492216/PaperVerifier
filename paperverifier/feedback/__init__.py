"""Feedback engine for applying verification findings to documents."""

from paperverifier.feedback.applier import (
    AppliedFeedback,
    FeedbackApplier,
    FeedbackChange,
    FeedbackConflictError,
)
from paperverifier.feedback.diff_generator import DiffGenerator

__all__ = [
    "AppliedFeedback",
    "FeedbackApplier",
    "FeedbackChange",
    "FeedbackConflictError",
    "DiffGenerator",
]
