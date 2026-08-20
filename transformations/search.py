"""Deterministic finite closure over explicit Stage-4 transformations."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from capabilities import (
    ArtifactLeaf,
    CapabilityCatalog,
    DiscoveryLayerCertificate,
)
from capabilities.implementation import _digest
from contracts import ArtifactDescriptor
from engine.runtime.identity import require_object_fields, strict_hash

from .model import TransformationCatalog, TransformationSpec


@dataclass(frozen=True)
class TransformationSearchLimits:
    """Hard bounds whose activation invalidates global completeness."""

    max_descriptor_states: int = 100_000
    max_transformations: int = 100_000
    max_depth: int = 64

    def __post_init__(self) -> None:
        for name in ("max_descriptor_states", "max_transformations", "max_depth"):
            value = getattr(self, name)
            if (isinstance(value, bool) or not isinstance(value, int)
                    or value < (1 if name == "max_descriptor_states" else 0)):
                qualifier = "positive" if name == "max_descriptor_states" \
                    else "non-negative"
                raise ValueError(f"{name} must be a {qualifier} integer")

    def to_dict(self) -> dict[str, int]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TransformationSearchLimits":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "TransformationSearchLimits")
        return cls(**raw)


class TransformationLimitCode(str, Enum):
    MAX_DESCRIPTOR_STATES = "MAX_DESCRIPTOR_STATES"
    MAX_TRANSFORMATIONS = "MAX_TRANSFORMATIONS"
    MAX_DEPTH = "MAX_DEPTH"


@dataclass(frozen=True)
class TransformationLimitReason:
    code: TransformationLimitCode
    limit: int
    subject_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.code, TransformationLimitCode):
            raise TypeError("transformation limit code is invalid")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) \
                or self.limit < 0:
            raise ValueError("transformation limit must be non-negative")
        if (not isinstance(self.subject_ids, tuple) or not self.subject_ids
                or any(not isinstance(item, str) or not item
                       for item in self.subject_ids)):
            raise TypeError("limit subject IDs must be a non-empty text tuple")
        if self.subject_ids != tuple(sorted(set(self.subject_ids))):
            raise ValueError("limit subject IDs must be unique and sorted")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "limit": self.limit,
            "subject_ids": list(self.subject_ids),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TransformationLimitReason":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "TransformationLimitReason")
        raw["code"] = TransformationLimitCode(raw["code"])
        if not isinstance(raw["subject_ids"], list):
            raise ValueError("TransformationLimitReason.subject_ids must be an array")
        raw["subject_ids"] = tuple(raw["subject_ids"])
        return cls(**raw)


@dataclass(frozen=True)
class ReachableDescriptorState:
    descriptor: ArtifactDescriptor
    depth: int
    seed: bool
    producer_transformation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.descriptor, ArtifactDescriptor):
            raise TypeError("reachable state descriptor is invalid")
        if isinstance(self.depth, bool) or not isinstance(self.depth, int) \
                or self.depth < 0:
            raise ValueError("reachable state depth must be non-negative")
        if type(self.seed) is not bool:
            raise TypeError("reachable state seed marker must be bool")
        if (not isinstance(self.producer_transformation_ids, tuple)
                or any(not isinstance(item, str) or not item
                       for item in self.producer_transformation_ids)):
            raise TypeError("producer transformation IDs must be a text tuple")
        if self.producer_transformation_ids != tuple(sorted(
                set(self.producer_transformation_ids))):
            raise ValueError("producer transformation IDs must be unique and sorted")
        if not self.seed and not self.producer_transformation_ids:
            raise ValueError("a non-seed state needs a producing transformation")

    @property
    def descriptor_id(self) -> str:
        return self.descriptor.descriptor_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "descriptor": self.descriptor.to_dict(),
            "depth": self.depth,
            "seed": self.seed,
            "producer_transformation_ids": list(
                self.producer_transformation_ids),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ReachableDescriptorState":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "ReachableDescriptorState")
        raw["descriptor"] = ArtifactDescriptor.from_dict(raw["descriptor"])
        if not isinstance(raw["producer_transformation_ids"], list):
            raise ValueError(
                "ReachableDescriptorState.producer_transformation_ids must be an array")
        raw["producer_transformation_ids"] = tuple(
            raw["producer_transformation_ids"])
        return cls(**raw)


@dataclass(frozen=True)
class TransformationTransition:
    transformation_spec_id: str
    depth: int
    input_descriptor_ids: tuple[str, ...]
    output_descriptor_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _digest(self.transformation_spec_id, "transition transformation_spec_id")
        if isinstance(self.depth, bool) or not isinstance(self.depth, int) \
                or self.depth < 1:
            raise ValueError("transition depth must be positive")
        for values, label in (
                (self.input_descriptor_ids, "input descriptor IDs"),
                (self.output_descriptor_ids, "output descriptor IDs")):
            if (not isinstance(values, tuple) or not values
                    or any(not isinstance(item, str) or not item for item in values)):
                raise TypeError(f"{label} must be a non-empty text tuple")

    def to_dict(self) -> dict[str, Any]:
        return {
            "transformation_spec_id": self.transformation_spec_id,
            "depth": self.depth,
            "input_descriptor_ids": list(self.input_descriptor_ids),
            "output_descriptor_ids": list(self.output_descriptor_ids),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TransformationTransition":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "TransformationTransition")
        for name in ("input_descriptor_ids", "output_descriptor_ids"):
            if not isinstance(raw[name], list):
                raise ValueError(f"TransformationTransition.{name} must be an array")
            raw[name] = tuple(raw[name])
        return cls(**raw)


class TransformationFrontierCode(str, Enum):
    MISSING_INPUT_DESCRIPTORS = "MISSING_INPUT_DESCRIPTORS"
    MAX_DESCRIPTOR_STATES = "MAX_DESCRIPTOR_STATES"
    MAX_TRANSFORMATIONS = "MAX_TRANSFORMATIONS"
    MAX_DEPTH = "MAX_DEPTH"


@dataclass(frozen=True)
class TransformationFrontierItem:
    transformation_spec_id: str
    code: TransformationFrontierCode
    missing_input_descriptor_ids: tuple[str, ...] = ()
    candidate_depth: int | None = None

    def __post_init__(self) -> None:
        _digest(self.transformation_spec_id, "frontier transformation_spec_id")
        if not isinstance(self.code, TransformationFrontierCode):
            raise TypeError("transformation frontier code is invalid")
        if (not isinstance(self.missing_input_descriptor_ids, tuple)
                or any(not isinstance(item, str) or not item
                       for item in self.missing_input_descriptor_ids)):
            raise TypeError("missing descriptor IDs must be a text tuple")
        if self.missing_input_descriptor_ids != tuple(sorted(
                set(self.missing_input_descriptor_ids))):
            raise ValueError("missing descriptor IDs must be unique and sorted")
        if self.candidate_depth is not None and (
                isinstance(self.candidate_depth, bool)
                or not isinstance(self.candidate_depth, int)
                or self.candidate_depth < 1):
            raise ValueError("candidate depth must be positive when present")
        if self.code is TransformationFrontierCode.MISSING_INPUT_DESCRIPTORS:
            if not self.missing_input_descriptor_ids or self.candidate_depth is not None:
                raise ValueError("missing-input frontier metadata is inconsistent")
        elif self.missing_input_descriptor_ids or self.candidate_depth is None:
            raise ValueError("limit frontier metadata is inconsistent")

    def to_dict(self) -> dict[str, Any]:
        return {
            "transformation_spec_id": self.transformation_spec_id,
            "code": self.code.value,
            "missing_input_descriptor_ids": list(
                self.missing_input_descriptor_ids),
            "candidate_depth": self.candidate_depth,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TransformationFrontierItem":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "TransformationFrontierItem")
        raw["code"] = TransformationFrontierCode(raw["code"])
        if not isinstance(raw["missing_input_descriptor_ids"], list):
            raise ValueError(
                "TransformationFrontierItem.missing_input_descriptor_ids "
                "must be an array")
        raw["missing_input_descriptor_ids"] = tuple(
            raw["missing_input_descriptor_ids"])
        return cls(**raw)


@dataclass(frozen=True)
class TransformationExpansion:
    expansion_id: str
    base_catalog_id: str
    transformation_catalog_id: str
    limits: TransformationSearchLimits
    states: tuple[ReachableDescriptorState, ...]
    transitions: tuple[TransformationTransition, ...]
    frontier: tuple[TransformationFrontierItem, ...]
    discovery_complete: bool
    limit_reasons: tuple[TransformationLimitReason, ...]
    augmented_catalog: CapabilityCatalog

    def __post_init__(self) -> None:
        for value, label in (
                (self.expansion_id, "transformation expansion_id"),
                (self.base_catalog_id, "base catalog_id"),
                (self.transformation_catalog_id, "transformation catalog_id")):
            _digest(value, label)
        if not isinstance(self.limits, TransformationSearchLimits):
            raise TypeError("transformation expansion limits are invalid")
        for values, expected, label in (
                (self.states, ReachableDescriptorState, "states"),
                (self.transitions, TransformationTransition, "transitions"),
                (self.frontier, TransformationFrontierItem, "frontier"),
                (self.limit_reasons, TransformationLimitReason, "limit reasons")):
            if (not isinstance(values, tuple)
                    or not all(isinstance(item, expected) for item in values)):
                raise TypeError(f"transformation expansion {label} is invalid")
        if tuple(sorted(self.states, key=lambda item: item.descriptor_id)) \
                != self.states:
            raise ValueError("reachable states must be sorted by descriptor ID")
        if tuple(sorted(self.transitions,
                        key=lambda item: item.transformation_spec_id)) \
                != self.transitions:
            raise ValueError("transitions must be sorted by transformation ID")
        if tuple(sorted(self.frontier,
                        key=lambda item: item.transformation_spec_id)) \
                != self.frontier:
            raise ValueError("frontier must be sorted by transformation ID")
        if tuple(sorted(self.limit_reasons, key=lambda item: item.code.value)) \
                != self.limit_reasons:
            raise ValueError("limit reasons must be sorted by code")
        if len({item.descriptor_id for item in self.states}) != len(self.states):
            raise ValueError("reachable descriptor states cannot repeat")
        if len({item.transformation_spec_id for item in self.transitions}) \
                != len(self.transitions):
            raise ValueError("transformation transitions cannot repeat")
        if type(self.discovery_complete) is not bool:
            raise TypeError("discovery_complete must be bool")
        if self.discovery_complete != (not self.limit_reasons):
            raise ValueError(
                "transformation completeness must agree with activated limits")
        if not isinstance(self.augmented_catalog, CapabilityCatalog):
            raise TypeError("augmented catalog is invalid")
        if self.expansion_id != self.expected_id():
            raise ValueError("transformation expansion identity does not verify")
        provenance = self.augmented_catalog.discovery_provenance
        if provenance.base_catalog_id != self.base_catalog_id:
            raise ValueError(
                "augmented catalog provenance names another base catalog")
        if self.discovery_layer() not in provenance.layers:
            raise ValueError(
                "augmented catalog omits its transformation discovery layer")

    @property
    def complete(self) -> bool:
        return self.discovery_complete

    @property
    def reachable_descriptor_ids(self) -> tuple[str, ...]:
        return tuple(item.descriptor_id for item in self.states)

    @property
    def reachable_transformation_spec_ids(self) -> tuple[str, ...]:
        return tuple(item.transformation_spec_id for item in self.transitions)

    def discovery_layer(self):
        """Return the exact certificate layer for this finite closure.

        The import is local to keep the transformation model independent of
        the resolution package while still making the safe hand-off the
        obvious API at their boundary.
        """
        return DiscoveryLayerCertificate.bind(
            "TRANSFORMATION_EXPANSION",
            self.expansion_id,
            source_ids=(self.transformation_catalog_id,),
            limits=self.limits.to_dict(),
            limit_reasons=self.limit_reasons,
            complete=self.discovery_complete,
        )

    def discovery_certificate(self):
        """Project the provenance already authenticated by catalog identity."""
        from resolution.upstream import DiscoveryCertificate

        return DiscoveryCertificate.for_catalog(self.augmented_catalog)

    def expected_id(self) -> str:
        return strict_hash(_expansion_payload(
            self.base_catalog_id, self.transformation_catalog_id, self.limits,
            self.states, self.transitions, self.frontier,
            self.discovery_complete, self.limit_reasons,
            self.augmented_catalog.content_id))

    def to_dict(self) -> dict[str, Any]:
        return {
            "expansion_id": self.expansion_id,
            "base_catalog_id": self.base_catalog_id,
            "transformation_catalog_id": self.transformation_catalog_id,
            "limits": self.limits.to_dict(),
            "states": [item.to_dict() for item in self.states],
            "transitions": [item.to_dict() for item in self.transitions],
            "frontier": [item.to_dict() for item in self.frontier],
            "discovery_complete": self.discovery_complete,
            "limit_reasons": [item.to_dict() for item in self.limit_reasons],
            "augmented_catalog": self.augmented_catalog.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TransformationExpansion":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "TransformationExpansion")
        raw["limits"] = TransformationSearchLimits.from_dict(raw["limits"])
        for name, parser in (
                ("states", ReachableDescriptorState.from_dict),
                ("transitions", TransformationTransition.from_dict),
                ("frontier", TransformationFrontierItem.from_dict),
                ("limit_reasons", TransformationLimitReason.from_dict)):
            if not isinstance(raw[name], list):
                raise ValueError(f"TransformationExpansion.{name} must be an array")
            raw[name] = tuple(parser(item) for item in raw[name])
        raw["augmented_catalog"] = CapabilityCatalog.from_dict(
            raw["augmented_catalog"])
        return cls(**raw)


@dataclass(frozen=True)
class TransformationDiscoveryReplay:
    """Independent deterministic replay inputs for one closure certificate.

    A catalog layer and its hashes can prove only internal integrity.  They do
    not prove that the closure algorithm actually explored the declared
    transformation catalog.  Resolution therefore receives these frozen
    inputs separately and reruns the finite closure before trusting a layer's
    completeness verdict.
    """

    replay_id: str
    base_catalog: CapabilityCatalog
    transformation_catalog: TransformationCatalog
    limits: TransformationSearchLimits
    seed_descriptors: tuple[ArtifactDescriptor, ...] = ()
    artifact_leaves: tuple[ArtifactLeaf, ...] = ()

    def __post_init__(self) -> None:
        _digest(self.replay_id, "transformation replay_id")
        if not isinstance(self.base_catalog, CapabilityCatalog):
            raise TypeError("transformation replay base catalog is invalid")
        if not isinstance(self.transformation_catalog, TransformationCatalog):
            raise TypeError("transformation replay catalog is invalid")
        if not isinstance(self.limits, TransformationSearchLimits):
            raise TypeError("transformation replay limits are invalid")
        if (not isinstance(self.seed_descriptors, tuple)
                or not all(isinstance(item, ArtifactDescriptor)
                           for item in self.seed_descriptors)):
            raise TypeError("transformation replay seeds must be descriptors")
        if (not isinstance(self.artifact_leaves, tuple)
                or not all(isinstance(item, ArtifactLeaf)
                           for item in self.artifact_leaves)):
            raise TypeError("transformation replay leaves are invalid")
        if self.replay_id != self.expected_id():
            raise ValueError("transformation replay identity does not verify")

    @classmethod
    def bind(
        cls,
        base_catalog: CapabilityCatalog,
        transformation_catalog: TransformationCatalog,
        *,
        limits: TransformationSearchLimits = TransformationSearchLimits(),
        seed_descriptors: Iterable[ArtifactDescriptor] = (),
        artifact_leaves: Iterable[ArtifactLeaf] = (),
    ) -> "TransformationDiscoveryReplay":
        seeds = tuple(sorted(
            seed_descriptors, key=lambda item: item.descriptor_id))
        leaves = tuple(sorted(
            artifact_leaves, key=lambda item: item.leaf_id))
        payload = {
            "schema": "stage8r-transformation-discovery-replay-v1",
            "base_catalog_id": base_catalog.catalog_id,
            "transformation_catalog_id": transformation_catalog.catalog_id,
            "limits": limits.to_dict(),
            "seed_descriptor_ids": [item.descriptor_id for item in seeds],
            "artifact_leaf_ids": [item.leaf_id for item in leaves],
        }
        return cls(
            strict_hash(payload), base_catalog, transformation_catalog,
            limits, seeds, leaves)

    def expected_id(self) -> str:
        return strict_hash({
            "schema": "stage8r-transformation-discovery-replay-v1",
            "base_catalog_id": self.base_catalog.catalog_id,
            "transformation_catalog_id": self.transformation_catalog.catalog_id,
            "limits": self.limits.to_dict(),
            "seed_descriptor_ids": [
                item.descriptor_id for item in self.seed_descriptors],
            "artifact_leaf_ids": [item.leaf_id for item in self.artifact_leaves],
        })

    def verify(
        self,
        catalog: CapabilityCatalog,
        layer: DiscoveryLayerCertificate,
    ) -> None:
        replayed = self.replay()
        if replayed.discovery_layer() != layer:
            raise ValueError(
                "transformation discovery layer disagrees with independent replay")
        if replayed.augmented_catalog != catalog:
            raise ValueError(
                "transformation discovery catalog disagrees with independent replay")

    def replay(self) -> TransformationExpansion:
        """Recompute this one exact catalog step from its trusted inputs.

        The resolver uses the returned predecessor/output pair to assemble a
        chain.  This is intentionally distinct from :meth:`verify`, whose
        direct-call contract still requires the replay output itself.
        """
        return expand_transform_catalog(
            self.base_catalog,
            self.transformation_catalog,
            seed_descriptors=self.seed_descriptors,
            artifact_leaves=self.artifact_leaves,
            limits=self.limits,
        )


def _expansion_payload(
        base_catalog_id: str, transformation_catalog_id: str,
        limits: TransformationSearchLimits,
        states: tuple[ReachableDescriptorState, ...],
        transitions: tuple[TransformationTransition, ...],
        frontier: tuple[TransformationFrontierItem, ...],
        discovery_complete: bool,
        limit_reasons: tuple[TransformationLimitReason, ...],
        augmented_catalog_content_id: str,
) -> dict[str, Any]:
    return {
        "schema": "stage8r-transformation-expansion-v2",
        "base_catalog_id": base_catalog_id,
        "transformation_catalog_id": transformation_catalog_id,
        "limits": limits.to_dict(),
        "states": [item.to_dict() for item in states],
        "transitions": [item.to_dict() for item in transitions],
        "frontier": [item.to_dict() for item in frontier],
        "discovery_complete": discovery_complete,
        "limit_reasons": [item.to_dict() for item in limit_reasons],
        "augmented_catalog_content_id": augmented_catalog_content_id,
    }


def expand_transform_catalog(
        base_catalog: CapabilityCatalog,
        transformation_specs: Iterable[TransformationSpec]
        | TransformationCatalog,
        *,
        seed_descriptors: Iterable[ArtifactDescriptor] = (),
        artifact_leaves: Iterable[ArtifactLeaf] = (),
        limits: TransformationSearchLimits = TransformationSearchLimits(),
) -> TransformationExpansion:
    """Compute finite forward reachability and return an ordinary catalog.

    A normal fixed point with transforms blocked on absent inputs is complete.
    Hitting any configured bound is not: the typed reason is persisted and a
    caller must combine this flag with Stage-3 discovery before authorizing a
    supposedly global selection.
    """
    if not isinstance(base_catalog, CapabilityCatalog):
        raise TypeError("base_catalog must be a CapabilityCatalog")
    if not isinstance(limits, TransformationSearchLimits):
        raise TypeError("limits must be TransformationSearchLimits")
    transform_catalog = (transformation_specs
                         if isinstance(transformation_specs,
                                       TransformationCatalog)
                         else TransformationCatalog.freeze(
                             tuple(transformation_specs)))
    seeds = tuple(seed_descriptors)
    leaves = tuple(artifact_leaves)
    if not all(isinstance(item, ArtifactDescriptor) for item in seeds):
        raise TypeError("seed_descriptors must contain ArtifactDescriptor values")
    if not all(isinstance(item, ArtifactLeaf) for item in leaves):
        raise TypeError("artifact_leaves must contain ArtifactLeaf values")

    seed_by_id: dict[str, ArtifactDescriptor] = {}
    for capability in base_catalog.capabilities:
        for port in capability.output_ports:
            _insert_descriptor(seed_by_id, port.descriptor)
    for descriptor in seeds:
        _insert_descriptor(seed_by_id, descriptor)
    for leaf in leaves:
        _insert_descriptor(seed_by_id, leaf.descriptor)

    all_seed_ids = tuple(sorted(seed_by_id))
    retained_seed_ids = all_seed_ids[:limits.max_descriptor_states]
    omitted_seed_ids = all_seed_ids[limits.max_descriptor_states:]
    descriptors = {item: seed_by_id[item] for item in retained_seed_ids}
    depths = {item: 0 for item in retained_seed_ids}
    selected: dict[str, tuple[TransformationSpec, int]] = {}

    changed = True
    while changed:
        changed = False
        for spec in transform_catalog.transformations:
            if not set(spec.input_descriptor_ids).issubset(descriptors):
                continue
            candidate_depth = max(
                depths[item] for item in spec.input_descriptor_ids) + 1
            if spec.spec_id in selected:
                _selected_spec, previous_depth = selected[spec.spec_id]
                if candidate_depth < previous_depth:
                    selected[spec.spec_id] = (spec, candidate_depth)
                    for port in spec.output_ports:
                        descriptor_id = port.descriptor.descriptor_id
                        if candidate_depth < depths[descriptor_id]:
                            depths[descriptor_id] = candidate_depth
                    changed = True
                continue
            if candidate_depth > limits.max_depth:
                continue
            if len(selected) >= limits.max_transformations:
                continue
            new_ids = tuple(item for item in spec.output_descriptor_ids
                            if item not in descriptors)
            if len(descriptors) + len(new_ids) > limits.max_descriptor_states:
                continue
            selected[spec.spec_id] = (spec, candidate_depth)
            for port in spec.output_ports:
                descriptor_id = port.descriptor.descriptor_id
                _insert_descriptor(descriptors, port.descriptor)
                depths[descriptor_id] = min(
                    depths.get(descriptor_id, candidate_depth), candidate_depth)
            changed = True

    transitions = tuple(sorted((
        TransformationTransition(
            spec.spec_id, depth, spec.input_descriptor_ids,
            spec.output_descriptor_ids)
        for spec, depth in selected.values()
    ), key=lambda item: item.transformation_spec_id))
    producers: dict[str, set[str]] = {}
    for transition in transitions:
        for descriptor_id in transition.output_descriptor_ids:
            producers.setdefault(descriptor_id, set()).add(
                transition.transformation_spec_id)
    states = tuple(
        ReachableDescriptorState(
            descriptors[descriptor_id], depths[descriptor_id],
            descriptor_id in retained_seed_ids,
            tuple(sorted(producers.get(descriptor_id, set()))),
        )
        for descriptor_id in sorted(descriptors)
    )

    frontier_items: list[TransformationFrontierItem] = []
    activated: dict[TransformationLimitCode, set[str]] = {}
    if omitted_seed_ids:
        activated.setdefault(
            TransformationLimitCode.MAX_DESCRIPTOR_STATES, set()).update(
                omitted_seed_ids)
    for spec in transform_catalog.transformations:
        if spec.spec_id in selected:
            continue
        missing = tuple(sorted(
            set(spec.input_descriptor_ids) - set(descriptors)))
        if missing:
            frontier_items.append(TransformationFrontierItem(
                spec.spec_id,
                TransformationFrontierCode.MISSING_INPUT_DESCRIPTORS,
                missing_input_descriptor_ids=missing,
            ))
            continue
        candidate_depth = max(
            depths[item] for item in spec.input_descriptor_ids) + 1
        if candidate_depth > limits.max_depth:
            frontier_code = TransformationFrontierCode.MAX_DEPTH
            limit_code = TransformationLimitCode.MAX_DEPTH
        elif len(selected) >= limits.max_transformations:
            frontier_code = TransformationFrontierCode.MAX_TRANSFORMATIONS
            limit_code = TransformationLimitCode.MAX_TRANSFORMATIONS
        else:
            frontier_code = TransformationFrontierCode.MAX_DESCRIPTOR_STATES
            limit_code = TransformationLimitCode.MAX_DESCRIPTOR_STATES
        frontier_items.append(TransformationFrontierItem(
            spec.spec_id, frontier_code, candidate_depth=candidate_depth))
        activated.setdefault(limit_code, set()).add(spec.spec_id)

    limit_values = {
        TransformationLimitCode.MAX_DESCRIPTOR_STATES:
            limits.max_descriptor_states,
        TransformationLimitCode.MAX_TRANSFORMATIONS:
            limits.max_transformations,
        TransformationLimitCode.MAX_DEPTH: limits.max_depth,
    }
    limit_reasons = tuple(sorted((
        TransformationLimitReason(
            code, limit_values[code], tuple(sorted(subject_ids)))
        for code, subject_ids in activated.items()
    ), key=lambda item: item.code.value))

    capability_by_id = {
        item.spec_id: item for item in base_catalog.capabilities}
    profile_by_id = {
        item.profile_id: item for item in base_catalog.execution_profiles}
    for spec, _depth in selected.values():
        capability = spec.to_capability_spec()
        previous_capability = capability_by_id.setdefault(
            capability.spec_id, capability)
        if previous_capability != capability:
            raise ValueError("capability identity collision during transformation expansion")
        profile = spec.execution_profile
        previous_profile = profile_by_id.setdefault(profile.profile_id, profile)
        if previous_profile != profile:
            raise ValueError("profile identity collision during transformation expansion")
    frontier = tuple(sorted(
        frontier_items, key=lambda item: item.transformation_spec_id))
    complete = not limit_reasons
    # The expansion record binds the exact spec/profile contents but not the
    # final catalog ID, because that ID in turn embeds this expansion record.
    # This two-level construction avoids a hash cycle while keeping both
    # directions replayable.
    content_catalog = CapabilityCatalog.freeze(
        capability_by_id.values(), profile_by_id.values())
    payload = _expansion_payload(
        base_catalog.catalog_id, transform_catalog.catalog_id, limits, states,
        transitions, frontier, complete, limit_reasons,
        content_catalog.content_id)
    expansion_id = strict_hash(payload)
    layer = DiscoveryLayerCertificate.bind(
        "TRANSFORMATION_EXPANSION",
        expansion_id,
        source_ids=(transform_catalog.catalog_id,),
        limits=limits.to_dict(),
        limit_reasons=limit_reasons,
        complete=complete,
    )
    augmented = CapabilityCatalog.freeze(
        capability_by_id.values(),
        profile_by_id.values(),
        discovery_base_catalog_id=base_catalog.catalog_id,
        discovery_layers=(*base_catalog.discovery_provenance.layers, layer),
    )
    return TransformationExpansion(
        expansion_id, base_catalog.catalog_id,
        transform_catalog.catalog_id, limits, states, transitions, frontier,
        complete, limit_reasons, augmented,
    )


def _insert_descriptor(target: dict[str, ArtifactDescriptor],
                       descriptor: ArtifactDescriptor) -> None:
    descriptor_id = descriptor.descriptor_id
    previous = target.setdefault(descriptor_id, descriptor)
    if previous != descriptor:  # defensive hash-collision guard
        raise ValueError("artifact descriptor identity collision")
