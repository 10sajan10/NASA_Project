"""Bounded, restartable metadata discovery with a real fixed point.

Discovery is a loop, not a single pass, because a producer's expansion can
reveal a data question that did not exist when planning started: you cannot ask
for terrain "on the grid the coarse source actually returned" until the coarse
source has answered.  The availability snapshot may only freeze once no new
query is produced.

Every bound that can stop this loop early — page caps, asset caps, query caps,
round caps, connector deadlines, provider cooldowns, exhausted quota — is typed
and recorded, and flows out through :attr:`AcquisitionExpansion.complete` into
the *existing* ``upstream_discovery_complete`` channel that Stage 4 added.
There is deliberately no second completeness mechanism.
"""
from __future__ import annotations

import dataclasses
import math
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable

from capabilities import DiscoveryLayerCertificate
from capabilities.implementation import _digest, _required_text
from contracts import BBoxSupport, TemporalKind, TemporalSupport
from contracts.identity import decimal_value
from engine.runtime.identity import require_object_fields, strict_hash

from .binding import BindingResult, BoundAssetManifest, bind_manifest
from .connector import (
    AssetCandidate,
    MetadataQuery,
    PermanentSourceError,
    SourceDescriptor,
    SourceConnector,
    TransientSourceError,
)
from .manifest import ManifestShardStore
from .session import (
    PlanningSessionStore,
    ProviderQuota,
    QuotaExceededError,
)


@dataclass(frozen=True)
class AcquisitionLimits:
    """Explicit discovery budgets. Activating any of them ends completeness."""

    max_pages_per_query: int = 50
    max_assets_per_query: int = 10_000
    max_queries: int = 64
    max_rounds: int = 8
    connector_deadline_s: float = 60.0

    def __post_init__(self) -> None:
        for name in ("max_pages_per_query", "max_assets_per_query",
                     "max_queries", "max_rounds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if (isinstance(self.connector_deadline_s, bool)
                or not isinstance(self.connector_deadline_s, (int, float))
                or not math.isfinite(self.connector_deadline_s)
                or self.connector_deadline_s <= 0):
            raise ValueError("connector_deadline_s must be finite and positive")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AcquisitionLimits":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "AcquisitionLimits")
        return cls(**raw)


class AcquisitionLimitCode(str, Enum):
    MAX_PAGES_PER_QUERY = "MAX_PAGES_PER_QUERY"
    MAX_ASSETS_PER_QUERY = "MAX_ASSETS_PER_QUERY"
    MAX_QUERIES = "MAX_QUERIES"
    MAX_ROUNDS = "MAX_ROUNDS"
    CONNECTOR_DEADLINE = "CONNECTOR_DEADLINE"
    PROVIDER_COOLDOWN = "PROVIDER_COOLDOWN"
    PROVIDER_QUOTA = "PROVIDER_QUOTA"
    CONNECTOR_UNAVAILABLE = "CONNECTOR_UNAVAILABLE"


@dataclass(frozen=True)
class AcquisitionLimitReason:
    code: AcquisitionLimitCode
    limit: int
    subject_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.code, AcquisitionLimitCode):
            raise TypeError("acquisition limit code is invalid")
        if isinstance(self.limit, bool) or not isinstance(self.limit, int) \
                or self.limit < 0:
            raise ValueError("acquisition limit must be non-negative")
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
    def from_dict(cls, value: dict[str, Any]) -> "AcquisitionLimitReason":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "AcquisitionLimitReason")
        raw["code"] = AcquisitionLimitCode(raw["code"])
        if not isinstance(raw["subject_ids"], list):
            raise ValueError("AcquisitionLimitReason.subject_ids must be an array")
        raw["subject_ids"] = tuple(raw["subject_ids"])
        return cls(**raw)


# -- second-order queries -------------------------------------------------


@dataclass(frozen=True)
class SecondOrderQuerySpec:
    """A declared follow-up question whose *parameters* are not yet known.

    The rule is a closed registry key, exactly like a binder or a unit
    conversion.  A caller cannot supply a callable, so a second-order query
    cannot become an arbitrary escape hatch into discovery.
    """

    rule_id: str
    trigger_query_id: str
    source_id: str
    concept_id: str
    schema_version: str
    units: str
    representation: str

    def __post_init__(self) -> None:
        _required_text(self.rule_id, "second-order rule_id")
        second_order_rule(self.rule_id)  # closed-registry check
        _digest(self.trigger_query_id, "second-order trigger_query_id")
        for value, label in (
                (self.source_id, "second-order source_id"),
                (self.concept_id, "second-order concept_id"),
                (self.schema_version, "second-order schema_version"),
                (self.units, "second-order units"),
                (self.representation, "second-order representation")):
            _required_text(value, label)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SecondOrderQuerySpec":
        raw = require_object_fields(
            value, {field.name for field in dataclasses.fields(cls)},
            "SecondOrderQuerySpec")
        return cls(**raw)


def _support_over_discovered_extent(
        spec: SecondOrderQuerySpec,
        candidates: tuple[AssetCandidate, ...]) -> MetadataQuery | None:
    """Ask for support data over the extent the trigger query actually returned.

    The requested region is *not* what the user asked for: it is the union of
    the tiles the provider really has, which is generally larger.  That union
    is unknowable before the first round, which is exactly why this query
    cannot be hoisted into round zero.
    """
    if not candidates:
        return None
    boxes = [item.extent.spatial for item in candidates]
    crs = boxes[0].crs
    axis_order = boxes[0].axis_order
    if any(box.crs != crs or box.axis_order != axis_order for box in boxes):
        return None
    bounds = [tuple(decimal_value(value) for value in box.bounds)
              for box in boxes]
    union = BBoxSupport(
        crs, axis_order,
        (str(min(item[0] for item in bounds)),
         str(min(item[1] for item in bounds)),
         str(max(item[2] for item in bounds)),
         str(max(item[3] for item in bounds))),
    )
    windows = [item.extent.temporal for item in candidates
               if item.extent.temporal.kind is not TemporalKind.TIME_INVARIANT]
    if windows:
        reference = windows[0]
        for field in ("kind", "cadence_s", "anchor", "max_gap_s",
                      "sample_semantics", "reference_time"):
            if any(getattr(window, field) != getattr(reference, field)
                   for window in windows):
                # Follow-up discovery may narrow facts returned by metadata;
                # it may never manufacture a common timeline where none was
                # established.
                return None
        temporal = TemporalSupport(
            reference.kind,
            start=min(window.start for window in windows),
            end=max(window.end for window in windows),
            cadence_s=reference.cadence_s,
            anchor=reference.anchor,
            max_gap_s=reference.max_gap_s,
            sample_semantics=reference.sample_semantics,
            reference_time=reference.reference_time,
        )
    else:
        temporal = TemporalSupport(TemporalKind.TIME_INVARIANT)
    return MetadataQuery(
        source_id=spec.source_id,
        concept_id=spec.concept_id,
        schema_version=spec.schema_version,
        units=spec.units,
        representation=spec.representation,
        spatial=union,
        temporal=temporal,
    )


_SECOND_ORDER_RULES: dict[
    str, Callable[[SecondOrderQuerySpec, tuple[AssetCandidate, ...]],
                  MetadataQuery | None]] = {
    "second_order.support_over_discovered_extent.v1":
        _support_over_discovered_extent,
}


def second_order_rule_keys() -> tuple[str, ...]:
    return tuple(sorted(_SECOND_ORDER_RULES))


def second_order_rule(key: str):
    try:
        return _SECOND_ORDER_RULES[key]
    except KeyError as exc:
        raise KeyError(f"unknown closed Stage-5 second-order rule {key!r}") from exc


# -- the discovery request and its result ---------------------------------


@dataclass(frozen=True)
class AcquisitionRequest:
    """One root query plus the binding target it must ultimately cover."""

    query: MetadataQuery
    target_spatial: BBoxSupport
    target_temporal: TemporalSupport
    halo: str = "0"
    bind: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.query, MetadataQuery):
            raise TypeError("acquisition request needs a MetadataQuery")
        if not isinstance(self.target_spatial, BBoxSupport):
            raise TypeError("acquisition target_spatial must be BBoxSupport")
        if not isinstance(self.target_temporal, TemporalSupport):
            raise TypeError("acquisition target_temporal must be TemporalSupport")
        if type(self.bind) is not bool:
            raise TypeError("acquisition request bind flag must be bool")

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query.to_dict(),
            "target_spatial": self.target_spatial.to_dict(),
            "target_temporal": self.target_temporal.to_dict(),
            "halo": str(self.halo),
            "bind": self.bind,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AcquisitionRequest":
        raw = require_object_fields(
            value,
            {"query", "target_spatial", "target_temporal", "halo", "bind"},
            "AcquisitionRequest",
        )
        raw["query"] = MetadataQuery.from_dict(raw["query"])
        raw["target_spatial"] = BBoxSupport.from_dict(raw["target_spatial"])
        raw["target_temporal"] = TemporalSupport.from_dict(
            raw["target_temporal"])
        return cls(**raw)


def _payload_sort_key(value: Any) -> str:
    return strict_hash(value.to_dict())


@dataclass(frozen=True)
class AcquisitionScope:
    """Pre-execution identity of one exact metadata-discovery universe.

    A complete result is meaningful only relative to the queries, second-order
    rules, connector/source-schema snapshots, limits, and shared quota policy
    that were actually searched.  Previously an empty result for two different
    query sets could have the same expansion ID.
    """

    scope_id: str
    requests: tuple[AcquisitionRequest, ...]
    second_order: tuple[SecondOrderQuerySpec, ...]
    source_descriptors: tuple[SourceDescriptor, ...]
    limits: AcquisitionLimits
    quota: ProviderQuota

    def __post_init__(self) -> None:
        _digest(self.scope_id, "acquisition scope_id")
        if (not isinstance(self.requests, tuple) or not self.requests
                or not all(isinstance(item, AcquisitionRequest)
                           for item in self.requests)):
            raise TypeError("acquisition scope needs typed root requests")
        if self.requests != tuple(sorted(
                self.requests, key=_payload_sort_key)):
            raise ValueError("acquisition scope requests must be canonical")
        query_ids = tuple(item.query.query_id for item in self.requests)
        if len(query_ids) != len(set(query_ids)):
            raise ValueError(
                "acquisition scope cannot bind one query identity twice")
        if (not isinstance(self.second_order, tuple)
                or not all(isinstance(item, SecondOrderQuerySpec)
                           for item in self.second_order)
                or self.second_order != tuple(sorted(
                    self.second_order, key=_payload_sort_key))):
            raise TypeError(
                "acquisition scope second-order rules must be canonical")
        rule_ids = tuple(_payload_sort_key(item) for item in self.second_order)
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError(
                "acquisition scope cannot bind one second-order rule twice")
        if (not isinstance(self.source_descriptors, tuple)
                or not self.source_descriptors
                or not all(isinstance(item, SourceDescriptor)
                           for item in self.source_descriptors)
                or self.source_descriptors != tuple(sorted(
                    self.source_descriptors, key=lambda item: item.source_id))):
            raise TypeError(
                "acquisition scope source descriptors must be canonical")
        source_ids = tuple(item.source_id for item in self.source_descriptors)
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("acquisition scope cannot repeat a source")
        if not isinstance(self.limits, AcquisitionLimits):
            raise TypeError("acquisition scope limits must be typed")
        if not isinstance(self.quota, ProviderQuota):
            raise TypeError("acquisition scope quota must be typed")
        if self.scope_id != self.expected_id():
            raise ValueError("acquisition scope identity does not verify")

    @classmethod
    def bind(
        cls,
        requests: Iterable[AcquisitionRequest],
        second_order: Iterable[SecondOrderQuerySpec],
        source_descriptors: Iterable[SourceDescriptor],
        limits: AcquisitionLimits,
        quota: ProviderQuota,
    ) -> "AcquisitionScope":
        request_values = tuple(sorted(requests, key=_payload_sort_key))
        rule_values = tuple(sorted(second_order, key=_payload_sort_key))
        source_values = tuple(sorted(
            source_descriptors, key=lambda item: item.source_id))
        payload = cls._payload(
            request_values, rule_values, source_values, limits, quota)
        return cls(
            strict_hash(payload), request_values, rule_values, source_values,
            limits, quota)

    @staticmethod
    def _payload(
        requests: tuple[AcquisitionRequest, ...],
        second_order: tuple[SecondOrderQuerySpec, ...],
        source_descriptors: tuple[SourceDescriptor, ...],
        limits: AcquisitionLimits,
        quota: ProviderQuota,
    ) -> dict[str, Any]:
        return {
            "schema": "stage8r-acquisition-scope-v1",
            "requests": [item.to_dict() for item in requests],
            "second_order": [item.to_dict() for item in second_order],
            "source_descriptors": [
                item.to_dict() for item in source_descriptors],
            "limits": limits.to_dict(),
            "quota": quota.to_dict(),
        }

    def expected_id(self) -> str:
        return strict_hash(self._payload(
            self.requests, self.second_order, self.source_descriptors,
            self.limits, self.quota))

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload(
            self.requests, self.second_order, self.source_descriptors,
            self.limits, self.quota)
        payload["scope_id"] = self.scope_id
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AcquisitionScope":
        raw = require_object_fields(
            value,
            {"schema", "scope_id", "requests", "second_order",
             "source_descriptors", "limits", "quota"},
            "AcquisitionScope",
        )
        if raw.pop("schema") != "stage8r-acquisition-scope-v1":
            raise ValueError("unsupported acquisition scope schema")
        for name, parser in (
                ("requests", AcquisitionRequest.from_dict),
                ("second_order", SecondOrderQuerySpec.from_dict),
                ("source_descriptors", SourceDescriptor.from_dict)):
            if not isinstance(raw[name], list):
                raise ValueError(f"AcquisitionScope.{name} must be an array")
            raw[name] = tuple(parser(item) for item in raw[name])
        raw["limits"] = AcquisitionLimits.from_dict(raw["limits"])
        raw["quota"] = ProviderQuota.from_dict(raw["quota"])
        return cls(**raw)


@dataclass(frozen=True)
class QueryOutcome:
    """What one executed query produced, including why it may have stopped."""

    query_id: str
    source_id: str
    round_index: int
    pages_read: int
    candidate_count: int
    exhausted: bool
    resumed: bool

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class AcquisitionExpansion:
    """The frozen availability snapshot for one planning session."""

    expansion_id: str
    session_id: str
    session_digest: str
    scope: AcquisitionScope
    limits: AcquisitionLimits
    outcomes: tuple[QueryOutcome, ...]
    bound_manifests: tuple[BoundAssetManifest, ...]
    binding_results: tuple[BindingResult, ...]
    discovery_complete: bool
    limit_reasons: tuple[AcquisitionLimitReason, ...]
    rounds: int

    def __post_init__(self) -> None:
        _digest(self.expansion_id, "acquisition expansion_id")
        _required_text(self.session_id, "acquisition session_id")
        _digest(self.session_digest, "acquisition session_digest")
        if not isinstance(self.scope, AcquisitionScope):
            raise TypeError("acquisition expansion scope is invalid")
        if self.scope.limits != self.limits:
            raise ValueError(
                "acquisition expansion limits disagree with its scope")
        if not isinstance(self.limits, AcquisitionLimits):
            raise TypeError("acquisition expansion limits are invalid")
        if type(self.discovery_complete) is not bool:
            raise TypeError("discovery_complete must be bool")
        if self.discovery_complete != (not self.limit_reasons):
            raise ValueError(
                "acquisition completeness must agree with activated limits")
        if tuple(sorted(self.limit_reasons, key=lambda item: item.code.value)) \
                != self.limit_reasons:
            raise ValueError("limit reasons must be sorted by code")
        if tuple(sorted(self.outcomes, key=lambda item: item.query_id)) \
                != self.outcomes:
            raise ValueError("query outcomes must be sorted by query ID")
        if isinstance(self.rounds, bool) or not isinstance(self.rounds, int) \
                or self.rounds < 0:
            raise ValueError("rounds must be a non-negative integer")
        if self.expansion_id != self.expected_id():
            raise ValueError("acquisition expansion identity does not verify")

    @property
    def complete(self) -> bool:
        """Effective completeness of *this* discovery layer."""
        return self.discovery_complete

    @property
    def limit_codes(self) -> tuple[str, ...]:
        return tuple(sorted({item.code.value for item in self.limit_reasons}))

    def expected_id(self) -> str:
        return strict_hash(self._payload(
            self.session_id, self.session_digest, self.scope, self.limits,
            self.bound_manifests, self.discovery_complete, self.limit_reasons))

    @staticmethod
    def _payload(session_id: str, session_digest: str,
                 scope: AcquisitionScope,
                 limits: AcquisitionLimits,
                 bound_manifests: tuple[BoundAssetManifest, ...],
                 discovery_complete: bool,
                 limit_reasons: tuple[AcquisitionLimitReason, ...],
                 ) -> dict[str, Any]:
        # Identity is the *availability snapshot* — what was found and whether
        # the search was whole — not the pagination history that produced it.
        # Resuming an interrupted search and finding the same assets must yield
        # the same snapshot, or a restart would look like a changed world.
        return {
            "schema": "stage8r-acquisition-expansion-v2",
            "session_id": session_id,
            "session_digest": session_digest,
            "scope_id": scope.scope_id,
            "limits": limits.to_dict(),
            "manifest_roots": sorted(
                item.manifest_root for item in bound_manifests),
            "discovery_complete": discovery_complete,
            "limit_reasons": [item.to_dict() for item in limit_reasons],
        }

    def manifest_for(self, query_id: str) -> BoundAssetManifest | None:
        for bound in self.bound_manifests:
            if bound.manifest.query_id == query_id:
                return bound
        return None

    def discovery_layer(self) -> DiscoveryLayerCertificate:
        """Project the exact, store-verifiable acquisition layer."""
        return DiscoveryLayerCertificate.bind(
            "ACQUISITION_EXPANSION",
            self.expansion_id,
            source_ids=tuple(
                item.source_id for item in self.scope.source_descriptors),
            limits=self.limits.to_dict(),
            limit_reasons=self.limit_reasons,
            complete=self.complete,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload(
            self.session_id, self.session_digest, self.scope, self.limits,
            self.bound_manifests, self.discovery_complete, self.limit_reasons)
        payload["expansion_id"] = self.expansion_id
        payload["scope"] = self.scope.to_dict()
        # Reported for attribution, deliberately outside the snapshot identity.
        payload["rounds"] = self.rounds
        payload["outcomes"] = [item.to_dict() for item in self.outcomes]
        return payload


@dataclass(frozen=True)
class AcquisitionDiscoveryReplay:
    """Trusted local-session replay for an acquisition completeness claim.

    This establishes completeness relative to the connector responses durably
    recorded in one local planning session.  It is not a provider signature or
    proof that an open-world remote catalog disclosed every possible asset.
    """

    expansion: AcquisitionExpansion
    session_store: PlanningSessionStore
    shard_store: ManifestShardStore

    def __post_init__(self) -> None:
        if not isinstance(self.expansion, AcquisitionExpansion):
            raise TypeError("acquisition replay expansion is invalid")
        if not isinstance(self.session_store, PlanningSessionStore):
            raise TypeError("acquisition replay session store is invalid")
        if not isinstance(self.shard_store, ManifestShardStore):
            raise TypeError("acquisition replay shard store is invalid")

    def verify(
        self,
        catalog: "CapabilityCatalog",
        layer: DiscoveryLayerCertificate,
    ) -> None:
        # Imports stay local so acquisition does not otherwise depend on the
        # capability catalog implementation.
        from capabilities import CapabilityCatalog

        if not isinstance(catalog, CapabilityCatalog):
            raise TypeError("acquisition replay needs a capability catalog")
        current_digest = self.session_store.session_digest(
            self.expansion.session_id)
        if current_digest != self.expansion.session_digest:
            raise ValueError(
                "acquisition session changed after its expansion was frozen")
        durable_scope = self.session_store.scope_for(
            self.expansion.session_id)
        if (durable_scope is None
                or durable_scope[0] != self.expansion.scope.scope_id
                or durable_scope[1] != self.expansion.scope.to_dict()):
            raise ValueError(
                "acquisition scope disagrees with its durable session")
        persisted_limits = set(self.session_store.limits_for(
            self.expansion.session_id))
        expected_limits = {
            (reason.code.value, subject)
            for reason in self.expansion.limit_reasons
            for subject in reason.subject_ids
        }
        if persisted_limits != expected_limits:
            raise ValueError(
                "acquisition limit record disagrees with its durable session")
        if self.expansion.complete and any(
                not item.exhausted for item in self.expansion.outcomes):
            raise ValueError(
                "complete acquisition has a non-exhausted durable query")

        # Reconstruct the complete query frontier from the frozen roots,
        # closed second-order rules and exact durable candidates.  This is the
        # load-bearing check that prevents a caller-authored expansion from
        # attaching forged manifests to a genuine session digest.
        requests = {
            item.query.query_id: item for item in self.expansion.scope.requests
        }
        rounds = {query_id: 1 for query_id in requests}
        changed = True
        while changed:
            changed = False
            for spec in self.expansion.scope.second_order:
                trigger = requests.get(spec.trigger_query_id)
                if trigger is None:
                    continue
                found = self.session_store.candidates_for(
                    self.expansion.session_id, spec.trigger_query_id)
                if not found:
                    continue
                follow_up = second_order_rule(spec.rule_id)(spec, found)
                if follow_up is None or follow_up.query_id in requests:
                    continue
                requests[follow_up.query_id] = AcquisitionRequest(
                    query=follow_up,
                    target_spatial=follow_up.spatial,
                    target_temporal=follow_up.temporal,
                    halo=trigger.halo,
                )
                rounds[follow_up.query_id] = (
                    rounds[spec.trigger_query_id] + 1)
                changed = True

        known_ids = self.session_store.known_query_ids(
            self.expansion.session_id)
        if any(query_id not in requests for query_id in known_ids):
            raise ValueError(
                "durable acquisition query is outside the frozen scope")
        outcomes_by_id = {
            item.query_id: item for item in self.expansion.outcomes
        }
        if self.expansion.complete:
            replayed_ids = set(requests)
            if (set(known_ids) != replayed_ids
                    or set(outcomes_by_id) != replayed_ids):
                raise ValueError(
                    "complete acquisition does not cover its entire replayed "
                    "query frontier")
            if any(not self.session_store.cursor_for(
                    self.expansion.session_id, query_id).exhausted
                    for query_id in replayed_ids):
                raise ValueError(
                    "complete acquisition contains an unexhausted query")
            if self.expansion.rounds != max(rounds.values(), default=0):
                raise ValueError(
                    "acquisition round count disagrees with replayed frontier")
        if any(query_id not in outcomes_by_id for query_id in known_ids):
            raise ValueError(
                "acquisition outcome omits a durable query")

        for query_id, outcome in outcomes_by_id.items():
            request = requests.get(query_id)
            if request is None:
                raise ValueError(
                    "acquisition outcome is outside the replayed frontier")
            if (outcome.source_id != request.query.source_id
                    or outcome.round_index != rounds[query_id]):
                raise ValueError(
                    "acquisition outcome source/round does not replay")
            state = self.session_store.cursor_for(
                self.expansion.session_id, query_id)
            found = self.session_store.candidates_for(
                self.expansion.session_id, query_id)
            if query_id in known_ids:
                if (self.session_store.query_payload_for(
                        self.expansion.session_id, query_id)
                        != request.query.to_dict()
                        or outcome.pages_read != state.pages_read
                        or outcome.candidate_count != len(found)
                        or outcome.exhausted != state.exhausted):
                    raise ValueError(
                        "acquisition outcome disagrees with durable pages")
            elif (outcome.pages_read != 0 or outcome.candidate_count != 0
                  or outcome.exhausted):
                raise ValueError(
                    "non-durable acquisition outcome claims provider results")

        descriptors = {
            item.source_id: item
            for item in self.expansion.scope.source_descriptors
        }
        replayed_results: list[BindingResult] = []
        replayed_bound: list[BoundAssetManifest] = []
        for query_id in sorted(outcomes_by_id):
            request = requests[query_id]
            if not request.bind:
                continue
            descriptor = descriptors.get(request.query.source_id)
            if descriptor is None:
                continue
            result = bind_manifest(
                self.session_store.candidates_for(
                    self.expansion.session_id, query_id),
                query=request.query,
                source_schema=descriptor.schema_for_query(request.query),
                store=self.shard_store,
                target_spatial=request.target_spatial,
                target_temporal=request.target_temporal,
                halo=request.halo,
            )
            replayed_results.append(result)
            if result.bound is not None:
                replayed_bound.append(result.bound)
        if tuple(replayed_results) != self.expansion.binding_results:
            raise ValueError(
                "acquisition binding results disagree with durable replay")
        if tuple(replayed_bound) != self.expansion.bound_manifests:
            raise ValueError(
                "acquisition manifests disagree with durable replay")

        authoritative_roots = {
            item.manifest_root for item in replayed_bound
        }
        catalog_roots = {
            item.acquisition_authority.bound_manifest["manifest"][
                "manifest_root"]
            for item in catalog.capabilities
            if item.acquisition_authority is not None
        }
        if catalog_roots != authoritative_roots:
            raise ValueError(
                "acquisition catalog capabilities do not exactly project "
                "the replayed manifests")
        if self.expansion.discovery_layer() != layer:
            raise ValueError(
                "acquisition discovery layer disagrees with session replay")
        if layer not in catalog.discovery_provenance.layers:
            raise ValueError(
                "acquisition discovery layer is absent from the final catalog")


class AcquisitionSearch:
    """Runs bounded, restartable, fixed-point metadata discovery."""

    def __init__(
        self,
        session_store: PlanningSessionStore,
        shard_store: ManifestShardStore,
        connectors: Iterable[SourceConnector],
        *,
        limits: AcquisitionLimits = AcquisitionLimits(),
        quota: ProviderQuota = ProviderQuota(),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(session_store, PlanningSessionStore):
            raise TypeError("search requires a PlanningSessionStore")
        if not isinstance(shard_store, ManifestShardStore):
            raise TypeError("search requires a ManifestShardStore")
        self.session_store = session_store
        self.shard_store = shard_store
        connector_values = tuple(connectors)
        if (not all(isinstance(value, SourceConnector)
                    for value in connector_values)):
            raise TypeError("search connectors must implement SourceConnector")
        if len({value.source_id for value in connector_values}) \
                != len(connector_values):
            raise ValueError("search cannot bind one source ID twice")
        self.connectors = {value.source_id: value for value in connector_values}
        if not self.connectors:
            raise ValueError("at least one connector is required")
        self.limits = limits
        self.quota = quota
        self._clock = clock

    def discover(
        self,
        session_id: str,
        requests: Iterable[AcquisitionRequest],
        *,
        second_order: Iterable[SecondOrderQuerySpec] = (),
    ) -> AcquisitionExpansion:
        """Search until no new query appears, then freeze the snapshot."""
        _required_text(session_id, "session_id")
        self.session_store.open_session(session_id)
        request_values = tuple(requests)
        if not request_values:
            raise ValueError("discovery needs at least one request")
        rules = tuple(second_order)

        scope = AcquisitionScope.bind(
            request_values,
            rules,
            (value.descriptor for value in self.connectors.values()),
            self.limits,
            self.quota,
        )
        # Bind before quota debit or connector invocation. A crash after a
        # partial page can then resume only under this exact frozen universe.
        self.session_store.bind_scope(
            session_id, scope.scope_id, scope.to_dict())
        # Execute the exact canonical ordering whose identity was frozen above.
        # If bounded discovery used caller iteration order while the scope hash
        # sorted it, two calls with the same scope could activate different
        # frontiers under max_queries/max_rounds.
        request_values = scope.requests
        rules = scope.second_order

        by_query: dict[str, AcquisitionRequest] = {
            item.query.query_id: item for item in request_values}
        if len(by_query) != len(request_values):
            raise ValueError(
                "one acquisition query identity cannot be requested twice")
        outcomes: dict[str, QueryOutcome] = {}
        candidates: dict[str, tuple[AssetCandidate, ...]] = {}
        # A planning-session identity is monotonic.  Once a bound has cut one
        # of its searches short, a restart may continue collecting a useful
        # incumbent but it may not quietly reinterpret that same session as a
        # complete availability snapshot.  The earlier implementation wrote
        # these rows but rebuilt ``activated`` from only the current call,
        # which made completeness depend on process history.
        activated: dict[AcquisitionLimitCode, set[str]] = {}
        for raw_code, subject in self.session_store.limits_for(session_id):
            try:
                code = AcquisitionLimitCode(raw_code)
            except ValueError as exc:
                raise RuntimeError(
                    "planning session contains an unknown persisted "
                    f"discovery limit {raw_code!r}") from exc
            activated.setdefault(code, set()).add(subject)

        pending = [item.query for item in request_values]
        executed: set[str] = set()
        rounds = 0

        while pending:
            if rounds >= self.limits.max_rounds:
                self._activate(
                    activated, AcquisitionLimitCode.MAX_ROUNDS,
                    tuple(item.query_id for item in pending), session_id)
                break
            rounds += 1
            for query in pending:
                if len(executed) >= self.limits.max_queries:
                    self._activate(
                        activated, AcquisitionLimitCode.MAX_QUERIES,
                        (query.query_id,), session_id)
                    continue
                executed.add(query.query_id)
                outcome, found = self._run_query(
                    session_id, query, rounds, activated)
                outcomes[query.query_id] = outcome
                candidates[query.query_id] = found

            # A round only ends when its results have been offered to every
            # declared second-order rule; a rule that yields a new query keeps
            # the snapshot open for another round.
            next_queries: list[MetadataQuery] = []
            for spec in rules:
                trigger = candidates.get(spec.trigger_query_id)
                if not trigger:
                    continue
                follow_up = second_order_rule(spec.rule_id)(spec, trigger)
                if follow_up is None or follow_up.query_id in executed:
                    continue
                source_request = by_query.get(spec.trigger_query_id)
                by_query[follow_up.query_id] = AcquisitionRequest(
                    query=follow_up,
                    target_spatial=follow_up.spatial,
                    target_temporal=follow_up.temporal,
                    halo=source_request.halo if source_request else "0",
                )
                next_queries.append(follow_up)
            pending = next_queries

        bound_manifests: list[BoundAssetManifest] = []
        binding_results: list[BindingResult] = []
        for query_id in sorted(outcomes):
            request = by_query[query_id]
            if not request.bind:
                continue
            connector = self.connectors.get(request.query.source_id)
            if connector is None:
                continue
            source_schema = connector.descriptor.schema_for_query(request.query)
            result = bind_manifest(
                candidates.get(query_id, ()),
                query=request.query,
                source_schema=source_schema,
                store=self.shard_store,
                target_spatial=request.target_spatial,
                target_temporal=request.target_temporal,
                halo=request.halo,
            )
            binding_results.append(result)
            if result.bound is not None:
                bound_manifests.append(result.bound)

        limit_values = {
            AcquisitionLimitCode.MAX_PAGES_PER_QUERY:
                self.limits.max_pages_per_query,
            AcquisitionLimitCode.MAX_ASSETS_PER_QUERY:
                self.limits.max_assets_per_query,
            AcquisitionLimitCode.MAX_QUERIES: self.limits.max_queries,
            AcquisitionLimitCode.MAX_ROUNDS: self.limits.max_rounds,
            AcquisitionLimitCode.CONNECTOR_DEADLINE:
                int(self.limits.connector_deadline_s),
            AcquisitionLimitCode.PROVIDER_COOLDOWN: 0,
            AcquisitionLimitCode.PROVIDER_QUOTA: 0,
            AcquisitionLimitCode.CONNECTOR_UNAVAILABLE: 0,
        }
        limit_reasons = tuple(sorted((
            AcquisitionLimitReason(code, limit_values[code],
                                   tuple(sorted(subjects)))
            for code, subjects in activated.items()
        ), key=lambda item: item.code.value))
        ordered_outcomes = tuple(
            outcomes[key] for key in sorted(outcomes))
        complete = not limit_reasons
        session_digest = self.session_store.session_digest(session_id)
        expansion_id = strict_hash(AcquisitionExpansion._payload(
            session_id, session_digest, scope, self.limits,
            tuple(bound_manifests), complete, limit_reasons))
        expansion = AcquisitionExpansion(
            expansion_id, session_id, session_digest, scope, self.limits,
            ordered_outcomes, tuple(bound_manifests), tuple(binding_results),
            complete, limit_reasons, rounds)
        # Only a *whole* search seals the session.  A truncated one leaves the
        # cursors open so a later pass can continue from them; its snapshot is
        # still usable, but it is explicitly incomplete rather than final.
        if complete:
            self.session_store.freeze_session(session_id, expansion.expansion_id)
        return expansion

    # -- internals --------------------------------------------------------

    def _activate(self, activated: dict[AcquisitionLimitCode, set[str]],
                  code: AcquisitionLimitCode, subjects: tuple[str, ...],
                  session_id: str) -> None:
        target = activated.setdefault(code, set())
        for subject in subjects:
            target.add(subject)
            self.session_store.record_limit(session_id, code.value, subject)

    def _run_query(self, session_id: str, query: MetadataQuery,
                   round_index: int,
                   activated: dict[AcquisitionLimitCode, set[str]],
                   ) -> tuple[QueryOutcome, tuple[AssetCandidate, ...]]:
        connector = self.connectors.get(query.source_id)
        state = self.session_store.cursor_for(session_id, query.query_id)
        resumed = state.pages_read > 0
        if connector is None:
            self._activate(activated, AcquisitionLimitCode.CONNECTOR_UNAVAILABLE,
                           (query.source_id,), session_id)
            return (QueryOutcome(query.query_id, query.source_id, round_index,
                                 state.pages_read, 0, False, resumed), ())
        # Refuse relabelling before spending provider quota or accepting any
        # provider metadata.  Query facts are authorized by a frozen schema,
        # not by whatever a caller puts in a request object.
        connector.descriptor.schema_for_query(query)
        if self.session_store.in_cooldown(query.source_id):
            self._activate(activated, AcquisitionLimitCode.PROVIDER_COOLDOWN,
                           (query.source_id,), session_id)
            stored = self.session_store.candidates_for(
                session_id, query.query_id)
            return (QueryOutcome(query.query_id, query.source_id, round_index,
                                 state.pages_read, len(stored), state.exhausted,
                                 resumed), stored)

        # An already-exhausted query is never re-paginated on restart: its
        # pages were paid for once and its results are durable.
        if state.exhausted:
            stored = self.session_store.candidates_for(
                session_id, query.query_id)
            return (QueryOutcome(query.query_id, query.source_id, round_index,
                                 state.pages_read, len(stored), True, resumed),
                    stored)

        cursor = state.cursor
        pages_read = state.pages_read
        exhausted = False
        started = self._clock()
        stored_count = len(self.session_store.candidates_for(
            session_id, query.query_id))

        while True:
            if pages_read >= self.limits.max_pages_per_query:
                self._activate(activated,
                               AcquisitionLimitCode.MAX_PAGES_PER_QUERY,
                               (query.query_id,), session_id)
                break
            if self._clock() - started > self.limits.connector_deadline_s:
                self._activate(activated,
                               AcquisitionLimitCode.CONNECTOR_DEADLINE,
                               (query.query_id,), session_id)
                break
            try:
                self.session_store.debit_quota(
                    query.source_id, self.quota, metadata_calls=1)
            except QuotaExceededError:
                self._activate(activated, AcquisitionLimitCode.PROVIDER_QUOTA,
                               (query.source_id,), session_id)
                break
            try:
                page = connector.search_metadata(
                    query, cursor, connector.descriptor.page_size)
            except TransientSourceError as exc:
                self.session_store.set_cooldown(
                    query.source_id, time.time() + 60.0, str(exc))
                self._activate(activated,
                               AcquisitionLimitCode.PROVIDER_COOLDOWN,
                               (query.source_id,), session_id)
                break
            except PermanentSourceError as exc:
                self.session_store.set_cooldown(
                    query.source_id, time.time() + 3600.0, str(exc))
                self._activate(activated,
                               AcquisitionLimitCode.CONNECTOR_UNAVAILABLE,
                               (query.source_id,), session_id)
                break

            admitted = page.candidates
            remaining = self.limits.max_assets_per_query - stored_count
            if len(admitted) > remaining:
                admitted = admitted[:max(remaining, 0)]
                self._activate(activated,
                               AcquisitionLimitCode.MAX_ASSETS_PER_QUERY,
                               (query.query_id,), session_id)
            cursor = page.next_cursor
            exhausted = page.exhausted
            stored_count += self.session_store.record_page(
                session_id, query.query_id, query.to_dict(), cursor=cursor,
                exhausted=exhausted, candidates=admitted)
            pages_read += 1
            if exhausted:
                break
            if stored_count >= self.limits.max_assets_per_query:
                self._activate(activated,
                               AcquisitionLimitCode.MAX_ASSETS_PER_QUERY,
                               (query.query_id,), session_id)
                break

        found = self.session_store.candidates_for(session_id, query.query_id)
        return (QueryOutcome(query.query_id, query.source_id, round_index,
                             pages_read, len(found), exhausted, resumed), found)


__all__ = [
    "AcquisitionExpansion",
    "AcquisitionLimitCode",
    "AcquisitionLimitReason",
    "AcquisitionLimits",
    "AcquisitionRequest",
    "AcquisitionScope",
    "AcquisitionDiscoveryReplay",
    "AcquisitionSearch",
    "QueryOutcome",
    "SecondOrderQuerySpec",
    "second_order_rule",
    "second_order_rule_keys",
]
