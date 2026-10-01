"""API version negotiation and lifecycle management.

Callers declare a version via the ``X-API-Version`` header. Missing header
falls back to the stable default version. Deprecated versions still work but
are marked in the response. Sunset or unknown versions are rejected with
distinct, machine-readable reasons (no silent fallback).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

VERSION_HEADER = "X-API-Version"
DEPRECATION_HEADER = "Deprecation"
SUNSET_HEADER = "Sunset"
DEFAULT_VERSION = "v2"


class VersionState(enum.Enum):
    STABLE = "stable"
    DEPRECATED = "deprecated"
    SUNSET = "sunset"


class RejectReason(enum.Enum):
    UNKNOWN_VERSION = "unknown_version"
    VERSION_SUNSET = "version_sunset"


@dataclass(frozen=True)
class VersionInfo:
    name: str
    state: VersionState
    sunset_date: str | None = None


@dataclass(frozen=True)
class NegotiationResult:
    """Outcome of version negotiation.

    Exactly one of ``version`` / ``reason`` is set:
    - accepted: ``version`` is the VersionInfo to serve under.
    - rejected: ``reason`` tells why (unknown vs sunset).
    """

    version: VersionInfo | None
    reason: RejectReason | None

    @property
    def accepted(self) -> bool:
        return self.version is not None


class VersionRegistry:
    """Holds the lifecycle state of every known API version."""

    def __init__(self, versions: dict[str, VersionInfo] | None = None) -> None:
        self._versions: dict[str, VersionInfo] = dict(versions or {})

    def negotiate(self, requested: str | None) -> NegotiationResult:
        """Resolve the caller-declared version to a serving decision.

        - ``None`` (header absent) -> stable default version.
        - known + stable/deprecated -> accepted (deprecated is marked later).
        - known + sunset -> rejected with VERSION_SUNSET.
        - anything else -> rejected with UNKNOWN_VERSION.
        """
        if requested is None or requested.strip() == "":
            return NegotiationResult(
                version=self._versions[DEFAULT_VERSION], reason=None
            )
        name = requested.strip()
        info = self._versions.get(name)
        if info is None:
            return NegotiationResult(version=None, reason=RejectReason.UNKNOWN_VERSION)
        if info.state is VersionState.SUNSET:
            return NegotiationResult(version=None, reason=RejectReason.VERSION_SUNSET)
        return NegotiationResult(version=info, reason=None)


def default_registry() -> VersionRegistry:
    """Registry with the built-in version lifecycle table."""
    return VersionRegistry(
        {
            "v1": VersionInfo("v1", VersionState.DEPRECATED, sunset_date="2026-12-31"),
            "v2": VersionInfo("v2", VersionState.STABLE),
            "v0": VersionInfo("v0", VersionState.SUNSET, sunset_date="2025-01-01"),
        }
    )
