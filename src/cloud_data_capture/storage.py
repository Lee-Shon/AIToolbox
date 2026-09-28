"""Durable, project-scoped business bytes for the candidate cloud gateway."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import uuid


_PROJECT = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


@dataclass(frozen=True)
class ArtifactRef:
    project: str
    sha256: str
    size: int
    path: str


class ArtifactWriter:
    def __init__(self, owner: "ManagedFiles", project: str):
        self.owner = owner
        self.project = project
        self.stage = owner.root / "staging" / (uuid.uuid4().hex + ".partial")
        self.stream = self.stage.open("xb")
        self.digest = hashlib.sha256()
        self.size = 0
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.closed:
            raise ValueError("artifact_writer_closed")
        if not data:
            return
        self.stream.write(data)
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.digest.update(data)
        self.size += len(data)

    def publish(self) -> ArtifactRef:
        if self.closed:
            raise ValueError("artifact_writer_closed")
        self.stream.flush()
        os.fsync(self.stream.fileno())
        self.stream.close()
        self.closed = True
        sha = self.digest.hexdigest()
        destination = self.owner.root / "objects" / self.project / sha[:2] / sha
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            # A hard link atomically publishes without replacing another result.
            os.link(self.stage, destination)
        except FileExistsError:
            digest = hashlib.sha256()
            size = 0
            with destination.open("rb") as existing:
                for block in iter(lambda: existing.read(1024 * 1024), b""):
                    digest.update(block)
                    size += len(block)
            if size != self.size or digest.hexdigest() != sha:
                raise RuntimeError("content_address_collision_or_corruption")
        self.stage.unlink()
        return ArtifactRef(self.project, sha, self.size, str(destination))

    def abort(self) -> None:
        if not self.closed:
            self.stream.close()
            self.closed = True
        self.stage.unlink(missing_ok=True)


class ManagedFiles:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        (self.root / "staging").mkdir(parents=True, exist_ok=True)
        (self.root / "objects").mkdir(parents=True, exist_ok=True)
        (self.root / "recovery").mkdir(parents=True, exist_ok=True)

    def begin(self, project: str) -> ArtifactWriter:
        if not _PROJECT.fullmatch(project):
            raise ValueError("invalid_project_id")
        return ArtifactWriter(self, project)

    def recovery(self, attempt_id: str, value: dict) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", attempt_id):
            raise ValueError("invalid_attempt_id")
        destination = self.root / "recovery" / (attempt_id + ".json")
        stage = self.root / "staging" / (uuid.uuid4().hex + ".recovery")
        with stage.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(stage, destination)
