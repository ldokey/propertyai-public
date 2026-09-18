"""Offline Stage B migration and reconciliation foundation.

This package never activates PostgreSQL business authority and never emits an
external effect.  It is deliberately separate from the Stage A runtime path.
"""

from .backfill import BackfillEngine, InMemoryMigrationRepository
from .identity import ROOT_NAMESPACE, deterministic_id
from .models import CanonicalRecord, SnapshotManifest, SourceObservation
from .reconciliation import ReconciliationEngine

__all__ = [
    "BackfillEngine",
    "CanonicalRecord",
    "InMemoryMigrationRepository",
    "ROOT_NAMESPACE",
    "ReconciliationEngine",
    "SnapshotManifest",
    "SourceObservation",
    "deterministic_id",
]
