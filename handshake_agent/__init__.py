"""NYUAD Handshake Career Pathway labeling agent."""

from .classifier import Classification, classify
from .labels import ALL_LABELS, HEALTH, MEDIA, SOCIAL, TECH

__all__ = [
    "Classification",
    "classify",
    "ALL_LABELS",
    "HEALTH",
    "MEDIA",
    "SOCIAL",
    "TECH",
]
