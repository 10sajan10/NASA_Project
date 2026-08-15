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
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable

from capabilities.implementation import _digest, _required_text
from contracts import BBoxSupport, TemporalKind, TemporalSupport
from contracts.identity import decimal_value
from engine.runtime.identity import require_object_fields, strict_hash

from .binding import BindingResult, BoundAssetManifest, bind_manifest
from .connector import (
    AssetCandidate,
    MetadataQuery,
    PermanentSourceError,
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
                or self.connector_deadline_s <= 0):
            raise ValueError("connector_deadline_s must be positive")

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
        temporal = TemporalSupport(
            windows[0].kind,
            start=min(window.start for window in windows),
            end=max(window.end for window in windows),
            sample_semantics=windows[0].sample_semantics,
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
            self.session_id, self.limits, self.bound_manifests,
            self.discovery_complete, self.limit_reasons))

    @staticmethod
    def _payload(session_id: str, limits: AcquisitionLimits,
                 bound_manifests: tuple[BoundAssetManifest, ...],
                 discovery_complete: bool,
                 limit_reasons: tuple[AcquisitionLimitReason, ...],
                 ) -> dict[str, Any]:
        # Identity is the *availability snapshot* — what was found and whether
        # the search was whole — not the pagination history that produced it.
        # Resuming an interrupted search and finding the same assets must yield
        # the same snapshot, or a restart would look like a changed world.
        return {
            "schema": "stage5-acquisition-expansion-v1",
            "session_id": session_id,
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

    def to_dict(self) -> dict[str, Any]:
        payload = self._payload(
            self.session_id, self.limits, self.bound_manifests,
            self.discovery_complete, self.limit_reasons)
        payload["expansion_id"] = self.expansion_id
        # Reported for attribution, deliberately outside the snapshot identity.
        payload["rounds"] = self.rounds
        payload["outcomes"] = [item.to_dict() for item in self.outcomes]
        return payload


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
        self.connectors = {value.source_id: value for value in connectors}
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

        by_query: dict[str, AcquisitionRequest] = {
            item.query.query_id: item for item in request_values}
        outcomes: dict[str, QueryOutcome] = {}
        candidates: dict[str, tuple[AssetCandidate, ...]] = {}
        activated: dict[AcquisitionLimitCode, set[str]] = {}

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
            result = bind_manifest(
                candidates.get(query_id, ()),
                query=request.query,
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
        expansion_id = strict_hash(AcquisitionExpansion._payload(
            session_id, self.limits, tuple(bound_manifests), complete,
            limit_reasons))
        expansion = AcquisitionExpansion(
            expansion_id, session_id, self.limits, ordered_outcomes,
            tuple(bound_manifests), tuple(binding_results), complete,
            limit_reasons, rounds)
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
    "AcquisitionSearch",
    "QueryOutcome",
    "SecondOrderQuerySpec",
    "second_order_rule",
    "second_order_rule_keys",
]
