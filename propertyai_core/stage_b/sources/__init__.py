"""Read-only Legacy source adapters used by convergent snapshot extraction."""

from .files import FileSnapshotSource, StableFile
from .notion import NotionPage, NotionSnapshotSource

__all__ = ["FileSnapshotSource", "NotionPage", "NotionSnapshotSource", "StableFile"]
