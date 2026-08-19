"""Automatic artifact discovery at the workflow-resolution boundary."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from capabilities import CapabilityCatalog, DeploymentCapabilitySnapshot
from contracts import EvidenceSnapshot, RequirementUse
from engine.runtime.identity import strict_hash
from resolution import (
    DiscoveryCertificate,
    DiscoveryLimits,
    DiscoveryUniverseContract,
    MilpSolveOptions,
    ResolutionOutcome,
    SelectionConstraints,
    WorkflowResolver,
)

from .records import ArtifactInput, ArtifactRecord, ArtifactRegistrySnapshot
from .registry import ArtifactRegistry


@dataclass(frozen=True)
class ArtifactWorkflowOutcome:
    """A resolved workflow plus exact selected native artifact pointers."""

    workflow_id: str
    resolution: ResolutionOutcome
    artifact_snapshot: ArtifactRegistrySnapshot
    selected_artifacts: tuple[ArtifactRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.resolution, ResolutionOutcome):
            raise TypeError("artifact workflow needs a ResolutionOutcome")
        if not isinstance(self.artifact_snapshot, ArtifactRegistrySnapshot):
            raise TypeError("artifact workflow needs a registry snapshot")
        if (not isinstance(self.selected_artifacts, tuple)
                or not all(isinstance(value, ArtifactRecord)
                           for value in self.selected_artifacts)):
            raise TypeError("selected artifacts must be immutable records")
        if self.selected_artifacts != tuple(sorted(
                self.selected_artifacts, key=lambda value: value.record_id)):
            raise ValueError("selected artifacts must be sorted")
        selected_leaf_ids = (
            set(self.resolution.selection.plan.selected_artifact_leaf_ids)
            if self.resolution.selection.plan is not None else set())
        observed_leaf_ids = {value.leaf.leaf_id
                             for value in self.selected_artifacts}
        if selected_leaf_ids != observed_leaf_ids:
            raise ValueError(
                "artifact workflow pointers do not match selected plan leaves")
        if self.workflow_id != self.expected_id():
            raise ValueError("artifact workflow identity does not verify")

    @classmethod
    def bind(
        cls,
        resolution: ResolutionOutcome,
        artifact_snapshot: ArtifactRegistrySnapshot,
    ) -> "ArtifactWorkflowOutcome":
        selected_ids = (
            tuple(resolution.selection.plan.selected_artifact_leaf_ids)
            if resolution.selection.plan is not None else ())
        records = tuple(sorted(
            (artifact_snapshot.record_for_leaf(value) for value in selected_ids),
            key=lambda value: value.record_id,
        ))
        payload = cls._payload(resolution, artifact_snapshot, records)
        return cls(strict_hash(payload), resolution, artifact_snapshot, records)

    @staticmethod
    def _payload(
        resolution: ResolutionOutcome,
        artifact_snapshot: ArtifactRegistrySnapshot,
        records: tuple[ArtifactRecord, ...],
    ) -> dict[str, Any]:
        return {
            "schema": "stage10a-artifact-workflow-v1",
            "resolution_id": resolution.resolution_id,
            "artifact_registry_snapshot_id": artifact_snapshot.snapshot_id,
            "selected_record_ids": [value.record_id for value in records],
        }

    def expected_id(self) -> str:
        return strict_hash(self._payload(
            self.resolution, self.artifact_snapshot,
            self.selected_artifacts))

    def to_dict(self) -> dict[str, Any]:
        plan = self.resolution.selection.plan
        return {
            "schema": "stage10a-artifact-workflow-v1",
            "workflow_id": self.workflow_id,
            "resolution_id": self.resolution.resolution_id,
            "status": self.resolution.status.value,
            "eligible_for_binding": self.resolution.eligible_for_binding,
            "artifact_registry_snapshot_id": self.artifact_snapshot.snapshot_id,
            "selected_invocation_ids": (
                list(plan.selected_invocation_ids) if plan is not None else []),
            "selected_artifacts": [value.to_dict()
                                   for value in self.selected_artifacts],
            "unresolved_root_use_ids": (
                [] if plan is not None
                else list(self.resolution.hypergraph.root_use_ids)),
        }


class ArtifactWorkflowResolver:
    """Refresh the artifact registry on every target-driven resolution.

    The service deliberately wraps rather than forks the Stage-3 resolver: the
    same compatibility proofs, global selector, completeness rules, and
    independent validator remain load-bearing.  This layer only supplies the
    current trusted artifact leaves and returns their native pointers.
    """

    def __init__(
        self,
        catalog: CapabilityCatalog,
        deployment_snapshot: DeploymentCapabilitySnapshot,
        artifact_registry: ArtifactRegistry,
        *,
        discovery_certificate: DiscoveryCertificate,
        discovery_universe: DiscoveryUniverseContract,
        discovery_replays: Iterable[object] = (),
        evidence_snapshot: EvidenceSnapshot | None = None,
        discovery_limits: DiscoveryLimits = DiscoveryLimits(),
        direct_only: bool = True,
    ) -> None:
        if not isinstance(artifact_registry, ArtifactRegistry):
            raise TypeError("artifact_registry must be ArtifactRegistry")
        if type(direct_only) is not bool:
            raise TypeError("direct_only must be bool")
        if direct_only:
            forbidden = tuple(
                value.spec_id for value in catalog.capabilities
                if (value.transformation_authority is not None
                    or value.acquisition_authority is not None))
            if forbidden:
                raise ValueError(
                    "direct-only artifact resolution refuses transformation "
                    "or acquisition-materialization capabilities: "
                    + ",".join(forbidden))
        self.catalog = catalog
        self.deployment_snapshot = deployment_snapshot
        self.artifact_registry = artifact_registry
        self.discovery_certificate = discovery_certificate
        self.discovery_universe = discovery_universe
        self.discovery_replays = tuple(discovery_replays)
        self.evidence_snapshot = evidence_snapshot
        self.discovery_limits = discovery_limits
        self.direct_only = direct_only

    def resolve(
        self,
        roots: Iterable[RequirementUse],
        *,
        constraints: SelectionConstraints | None = None,
        solve_options: MilpSolveOptions | None = None,
        require_proven_optimal: bool = True,
    ) -> ArtifactWorkflowOutcome:
        snapshot = self.artifact_registry.snapshot()
        leaves, availability = snapshot.planning_inputs()
        resolution = WorkflowResolver(
            self.catalog,
            self.deployment_snapshot,
            discovery_certificate=self.discovery_certificate,
            discovery_universe=self.discovery_universe,
            discovery_replays=self.discovery_replays,
            artifact_leaves=leaves,
            availability_snapshot=availability,
            evidence_snapshot=self.evidence_snapshot,
            discovery_limits=self.discovery_limits,
        ).resolve(
            tuple(roots),
            constraints=constraints,
            solve_options=solve_options,
            require_proven_optimal=require_proven_optimal,
        )
        return ArtifactWorkflowOutcome.bind(resolution, snapshot)

    def attach_output_registry(self, publisher: Any) -> Any:
        """Attach this request-time registry to a DatasetRef publisher.

        ``Cube`` exposes the deliberately small ``artifact_registry`` hook.
        Keeping attachment explicit prevents a legacy output with incomplete
        metadata from being silently promoted, while one configured service
        makes every subsequent typed DatasetRef arrival automatic.
        """
        if not hasattr(publisher, "artifact_registry"):
            raise TypeError(
                "artifact publisher must expose an artifact_registry hook")
        existing = publisher.artifact_registry
        if existing is not None and existing is not self.artifact_registry:
            raise ValueError("publisher is attached to another artifact registry")
        publisher.artifact_registry = self.artifact_registry
        return publisher

    def register_output(
        self,
        output: Any,
        *,
        producer_id: str,
        inputs: Iterable[ArtifactInput] = (),
    ) -> ArtifactRecord:
        """Register a typed DatasetRef emitted by a producer, without copying."""
        from cube.entries import DatasetRef
        if not isinstance(output, DatasetRef):
            raise TypeError("automatic artifact output must be a DatasetRef")
        if output.descriptor is None:
            raise ValueError(
                "automatic artifact registration requires a full descriptor")
        return self.artifact_registry.register_file(
            output.path,
            output.descriptor,
            media_type=output.media_type,
            producer_id=producer_id,
            producer_version=output.producer_version,
            output_port_id=output.output_port_id,
            inputs=inputs,
            evidence_profile_id=output.evidence_profile_id,
            metadata=output.detail,
        )

    def output_arrived(
        self,
        *,
        producer_id: str,
        outputs: dict[str, Any],
        inputs: Iterable[ArtifactInput] = (),
    ) -> tuple[ArtifactRecord, ...]:
        """Native-output event hook used by a producer/runtime boundary.

        The mapping key is the declared output port. Every value must be a
        typed DatasetRef naming the same port. Registration completes before
        the event returns, so the next resolution observes all outputs.
        """
        if not isinstance(outputs, dict) or not outputs:
            raise ValueError("artifact output event requires a non-empty mapping")
        input_values = tuple(inputs)
        prepared: list[ArtifactRecord] = []
        for port_id in sorted(outputs):
            if not isinstance(port_id, str) or not port_id:
                raise ValueError("artifact output event ports must be text")
            output = outputs[port_id]
            from cube.entries import DatasetRef
            if not isinstance(output, DatasetRef):
                raise TypeError("artifact output event values must be DatasetRef")
            if output.output_port_id != port_id:
                raise ValueError(
                    "DatasetRef output_port_id disagrees with event port")
            if output.descriptor is None:
                raise ValueError(
                    "automatic artifact registration requires a full descriptor")
            prepared.append(self.artifact_registry.prepare_file(
                output.path,
                output.descriptor,
                media_type=output.media_type,
                producer_id=producer_id,
                producer_version=output.producer_version,
                output_port_id=output.output_port_id,
                inputs=input_values,
                evidence_profile_id=output.evidence_profile_id,
                metadata=output.detail,
            ))
        return self.artifact_registry.register_records(prepared)


__all__ = ["ArtifactWorkflowOutcome", "ArtifactWorkflowResolver"]
