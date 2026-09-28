"""Isolated V10 cloud capture candidate; it is not a deployed gateway."""

from .proxy import CandidateServer, Caller, Provider, recover_incomplete
from .storage import ArtifactRef, ManagedFiles

__all__ = ["ArtifactRef", "Caller", "CandidateServer", "ManagedFiles", "Provider", "recover_incomplete"]
