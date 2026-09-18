from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..models import SnapshotIntegrityError, SourceObservation
from ..normalize import semantic_hash


def _stat_fingerprint(stat: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_mode,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


@dataclass(frozen=True)
class StableFile:
    path: Path
    original_path: Path
    resolved_path: Path
    original_lstat_identity: tuple[int, int, int, int, int, int]
    resolved_stat_identity: tuple[int, int, int, int, int, int]
    content: bytes
    content_hash: str

    @property
    def path_identity_hash(self) -> str:
        return semantic_hash(
            {
                "original_path": str(self.original_path),
                "resolved_path": str(self.resolved_path),
                "original_lstat_identity": self.original_lstat_identity,
                "resolved_stat_identity": self.resolved_stat_identity,
            }
        )


def read_stable_file(path: Path) -> StableFile:
    """Detect content writes, replacement, original-path swaps, and symlink swaps."""
    original_path = Path(os.path.abspath(os.fspath(path)))
    try:
        original_before = os.lstat(original_path)
        resolved_before = original_path.resolve(strict=True)
        target_before = os.stat(resolved_before, follow_symlinks=False)
    except OSError as error:
        raise SnapshotIntegrityError(f"source path cannot be resolved safely: {original_path}") from error

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(resolved_before, flags)
    try:
        fd_before = os.fstat(descriptor)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        fd_after = os.fstat(descriptor)
        original_after = os.lstat(original_path)
        resolved_after = original_path.resolve(strict=True)
        target_after = os.stat(resolved_after, follow_symlinks=False)
    except OSError as error:
        raise SnapshotIntegrityError(f"source path changed during read: {original_path}") from error
    finally:
        os.close(descriptor)

    original_identity = _stat_fingerprint(original_before)
    target_identity = _stat_fingerprint(target_before)
    if original_identity != _stat_fingerprint(original_after):
        raise SnapshotIntegrityError(f"original source path changed during read: {original_path}")
    if resolved_before != resolved_after:
        raise SnapshotIntegrityError(f"source symlink target changed during read: {original_path}")
    if not (
        target_identity
        == _stat_fingerprint(fd_before)
        == _stat_fingerprint(fd_after)
        == _stat_fingerprint(target_after)
    ):
        raise SnapshotIntegrityError(f"file changed or was replaced during read: {original_path}")
    content = b"".join(chunks)
    if len(content) != fd_after.st_size:
        raise SnapshotIntegrityError(f"file size changed during read: {original_path}")
    return StableFile(
        path=resolved_before,
        original_path=original_path,
        resolved_path=resolved_before,
        original_lstat_identity=original_identity,
        resolved_stat_identity=target_identity,
        content=content,
        content_hash=hashlib.sha256(content).hexdigest(),
    )


def _directory_evidence(root: Path) -> str:
    absolute = Path(os.path.abspath(os.fspath(root)))
    try:
        members = []
        for member in sorted(absolute.iterdir(), key=lambda item: item.name):
            lst = os.lstat(member)
            resolved = member.resolve(strict=True)
            target = os.stat(resolved, follow_symlinks=False)
            members.append(
                {
                    "name": member.name,
                    "path": str(member),
                    "lstat": _stat_fingerprint(lst),
                    "resolved": str(resolved),
                    "target": _stat_fingerprint(target),
                }
            )
    except OSError as error:
        raise SnapshotIntegrityError(f"authoritative directory changed during scan: {absolute}") from error
    return semantic_hash({"directory": str(absolute), "members": members})


class FileSnapshotSource:
    def __init__(
        self,
        source_type: str,
        paths: Mapping[str, Path],
        *,
        semantic_normalizer: Callable[[str, Any], Mapping[str, Any]],
        runtime_reference: str,
        authoritative_directories: Sequence[Path] = (),
    ) -> None:
        self.source_type = source_type
        self.paths = {identity: Path(path) for identity, path in paths.items()}
        self._semantic_normalizer = semantic_normalizer
        self.runtime_reference = runtime_reference
        self.authoritative_directories = tuple(Path(path) for path in authoritative_directories)
        self._last_files: dict[str, StableFile] = {}
        self._last_directory_hashes: dict[str, str] = {}

    def _read(self, identity: str) -> tuple[StableFile, SourceObservation]:
        stable = read_stable_file(self.paths[identity])
        try:
            decoded = json.loads(stable.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SnapshotIntegrityError(f"invalid JSON source file: {stable.original_path}") from error
        observation = SourceObservation(
            source_type=self.source_type,
            durable_identity=identity,
            semantic_payload=self._semantic_normalizer(identity, decoded),
            source_ref=str(stable.original_path),
            content_hash=stable.content_hash,
        )
        return stable, observation

    def scan(self) -> tuple[SourceObservation, ...]:
        before = {
            str(Path(os.path.abspath(os.fspath(root)))): _directory_evidence(root)
            for root in self.authoritative_directories
        }
        observations: list[SourceObservation] = []
        self._last_files = {}
        for identity in sorted(self.paths):
            stable, observation = self._read(identity)
            self._last_files[observation.row_key] = stable
            observations.append(observation)
        after = {
            str(Path(os.path.abspath(os.fspath(root)))): _directory_evidence(root)
            for root in self.authoritative_directories
        }
        if before != after:
            raise SnapshotIntegrityError("authoritative directory membership changed during scan")
        self._last_directory_hashes = after
        return tuple(observations)

    def point_read(self, durable_identity: str) -> SourceObservation | None:
        if durable_identity not in self.paths:
            return None
        _, observation = self._read(durable_identity)
        return observation

    @property
    def file_content_hashes(self) -> Mapping[str, str]:
        evidence: dict[str, str] = {}
        for key, value in self._last_files.items():
            evidence[key] = value.content_hash
            evidence[f"path:{key}"] = value.path_identity_hash
        for root, digest in self._last_directory_hashes.items():
            evidence[f"directory:{root}"] = digest
        return evidence


__all__ = ["FileSnapshotSource", "StableFile", "read_stable_file"]
