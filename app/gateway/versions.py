"""API version negotiation and lifecycle policy."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class VersionStatus(str, Enum):
    STABLE = "stable"
    DEPRECATED = "deprecated"
    RETIRED = "retired"


@dataclass(frozen=True)
class VersionPolicy:
    """Static lifecycle table describing every advertised API version."""

    default_version: str
    header_name: str
    versions: dict[str, VersionStatus]


@dataclass(frozen=True)
class VersionDecision:
    """Outcome of negotiating one request's requested version."""

    requested: str | None
    effective: str
    status: VersionStatus
    used_default: bool


def default_policy() -> VersionPolicy:
    """Factory for the gateway's default version policy.

    Lifecycle table (date-based versions, e.g. ``2025-01-01``):

    - ``2023-01-01`` RETIRED    - shut down, rejected with ``version_retired``
    - ``2024-01-01`` DEPRECATED - still served, every response is flagged
    - ``2025-01-01`` STABLE     - current default for undeclared callers
    """
    return VersionPolicy(
        default_version="2025-01-01",
        header_name="x-api-version",
        versions={
            "2023-01-01": VersionStatus.RETIRED,
            "2024-01-01": VersionStatus.DEPRECATED,
            "2025-01-01": VersionStatus.STABLE,
        },
    )


def negotiate(requested: str | None, policy: VersionPolicy) -> VersionDecision:
    """Resolve the caller-declared version against the lifecycle table.

    - undeclared -> stable default (never a silent fallback in other cases)
    - deprecated -> accepted, flagged
    - retired / unknown -> distinct rejection reasons
    """
    # Import locally to keep this module independent at import time.
    from app.gateway.errors import ErrorCode, GatewayError

    if requested is None or not requested.strip():
        return VersionDecision(
            requested=None,
            effective=policy.default_version,
            status=policy.versions[policy.default_version],
            used_default=True,
        )

    version = requested.strip()
    if version not in policy.versions:
        raise GatewayError(
            ErrorCode.VERSION_UNKNOWN,
            f"API version '{version}' is not a known version; no fallback is "
            f"applied; declare one of {sorted(policy.versions)} or omit the "
            f"version header to use the stable default "
            f"'{policy.default_version}'.",
            status_code=400,
            details={"requested_version": version,
                     "known_versions": sorted(policy.versions),
                     "default_version": policy.default_version},
        )

    status = policy.versions[version]
    if status is VersionStatus.RETIRED:
        raise GatewayError(
            ErrorCode.VERSION_RETIRED,
            f"API version '{version}' has been retired and can no longer be "
            f"used; please migrate to the stable default "
            f"'{policy.default_version}'.",
            status_code=410,
            details={"requested_version": version,
                     "default_version": policy.default_version},
        )

    return VersionDecision(requested=version, effective=version,
                           status=status, used_default=False)
