"""Lower only verified post-fetch content to an ordinary capability.

Metadata manifests are availability claims, not executable inputs.  Stage 8R
therefore forbids lowering a :class:`BoundAssetManifest` or a caller-authored
descriptor.  The only authority accepted here is a
:class:`FetchedContentBinding`, whose identity already includes the receipt,
ordered blob hashes and sizes, frozen source schema, proven coverage, and
derived descriptor.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from capabilities import (
    AcquisitionAuthority,
    BinderRef,
    BindingParameterization,
    BoundInvocation,
    BoundOutputPort,
    CapabilitySpec,
    DescriptorTemplate,
    ExecutionProfile,
    ParameterField,
    ParameterKind,
    ParameterSchema,
)
from capabilities.implementation import _digest
from contracts import ArtifactDescriptor
from engine.runtime.identity import (
    require_object_fields,
    strict_copy,
    strict_hash,
)

from .content import FetchedContentBinding
from .binding import BoundAssetManifest
from .coverage import assess_coverage
from .fetch import FetchedAsset
from .manifest import AssetRef, ManifestShard
from .schema import AssemblyMode, SourceSchema

ACQUISITION_BINDER_KEY = "acquisition.materialize.bind.v1"
ACQUISITION_OPERATION_KEY = "acquisition.materialize.v1"


def _lowering_rule_implementation_sha256() -> str:
    """Bind acquisition certificates to the current closed lowering rule."""
    identity = strict_hash({
        "schema": "stage8r-acquisition-lowering-rule-v1",
        "operation_key": ACQUISITION_OPERATION_KEY,
        "binder_key": ACQUISITION_BINDER_KEY,
        "input_ports": [],
        "output_ports": ["result"],
        "parameter_schema": ParameterSchema((
            ParameterField("content_binding", ParameterKind.JSON),
        )).to_dict(),
    })
    return hashlib.sha256(
        identity.encode("ascii") + b"\0" + Path(__file__).read_bytes(),
    ).hexdigest()


def _verified_content_binding_record(
        value: dict[str, Any],
) -> tuple[dict[str, Any], ArtifactDescriptor]:
    """Replay the strict content-addressed portion of a fetched binding.

    Storage receipt and blob verification occurs when ``FetchedContentBinding``
    is first minted and again in the runtime materializer.  This pure planning
    replay verifies the complete serialized identity and canonical projections
    needed to prevent descriptor/source-schema relabelling.
    """
    raw = require_object_fields(
        value,
        {"schema", "binding_id", "manifest_root", "source_id", "receipt_id",
         "content_root", "assets", "source_schema_id",
         "coverage_contract_id", "assembly_mode", "descriptor"},
        "acquisition authority content binding",
    )
    if raw["schema"] != "stage8r-fetched-content-binding-v1":
        raise ValueError("unknown fetched content binding schema")
    for field in (
            "binding_id", "manifest_root", "receipt_id", "content_root",
            "source_schema_id", "coverage_contract_id"):
        _digest(raw[field], f"fetched content {field}")
    if not isinstance(raw["source_id"], str) or not raw["source_id"].strip():
        raise ValueError("fetched content source_id must be non-empty text")
    if not isinstance(raw["assets"], list) or not raw["assets"]:
        raise ValueError("fetched content assets must be a non-empty array")
    assets = tuple(FetchedAsset.from_dict(item) for item in raw["assets"])
    asset_ids = tuple(item.asset_id for item in assets)
    if asset_ids != tuple(sorted(set(asset_ids))):
        raise ValueError("fetched content assets must be unique and sorted")
    expected_content_root = strict_hash({
        "schema": "stage8r-fetched-content-root-v1",
        "assets": [item.to_dict() for item in assets],
    })
    if raw["content_root"] != expected_content_root:
        raise ValueError("fetched content root does not verify")
    AssemblyMode(raw["assembly_mode"])
    descriptor = ArtifactDescriptor.from_dict(raw["descriptor"])
    identity_payload = strict_copy(raw)
    binding_id = identity_payload.pop("binding_id")
    if strict_hash(identity_payload) != binding_id:
        raise ValueError("fetched content binding identity does not verify")
    return raw, descriptor


def _verified_authority_contract(
        authority: AcquisitionAuthority,
) -> tuple[dict[str, Any], ArtifactDescriptor, ExecutionProfile]:
    if not isinstance(authority, AcquisitionAuthority):
        raise TypeError("acquisition authority is invalid")
    if authority.authority_id != authority.expected_id():
        raise ValueError("acquisition authority identity does not verify")
    expected_rule = _lowering_rule_implementation_sha256()
    if authority.lowering_rule_implementation_sha256 != expected_rule:
        raise ValueError(
            "acquisition lowering-rule implementation is stale or forged")
    binding, descriptor = _verified_content_binding_record(
        strict_copy(authority.content_binding))
    source_schema = SourceSchema.from_dict(
        strict_copy(authority.source_schema))
    bound = BoundAssetManifest.from_dict(
        strict_copy(authority.bound_manifest))
    if not isinstance(authority.manifest_assets, tuple):
        raise TypeError("acquisition authority manifest assets are invalid")
    manifest_assets = tuple(AssetRef.from_dict(item)
                            for item in strict_copy(authority.manifest_assets))
    if source_schema != bound.source_schema:
        raise ValueError(
            "acquisition authority source schema disagrees with bound manifest")
    if source_schema.schema_id != authority.source_schema_id:
        raise ValueError("acquisition authority source schema ID does not verify")
    if bound.coverage_contract_id != authority.coverage_contract_id:
        raise ValueError(
            "acquisition authority coverage contract ID does not verify")
    if binding["source_schema_id"] != source_schema.schema_id:
        raise ValueError(
            "acquisition authority names another source schema")
    if (binding["manifest_root"] != bound.manifest_root
            or binding["source_id"] != bound.source_id
            or binding["coverage_contract_id"]
            != bound.coverage_contract_id
            or binding["assembly_mode"]
            != source_schema.assembly_mode.value):
        raise ValueError(
            "fetched content binding disagrees with its bound manifest proof")
    manifest_ids = tuple(item.asset_id for item in manifest_assets)
    fetched_assets = tuple(
        FetchedAsset.from_dict(item) for item in binding["assets"])
    if (manifest_ids != bound.asset_ids
            or manifest_ids != tuple(item.asset_id for item in fetched_assets)):
        raise ValueError(
            "fetched content asset set/order disagrees with bound coverage")
    shards = tuple(
        ManifestShard(index, tuple(manifest_assets[start:start +
                                                   bound.manifest.shard_size]))
        for index, start in enumerate(range(
            0, len(manifest_assets), bound.manifest.shard_size)))
    if tuple(item.shard_digest for item in shards) \
            != bound.manifest.shard_digests:
        raise ValueError(
            "acquisition authority manifest rows do not verify shard digests")
    if (len(manifest_assets) != bound.manifest.asset_count
            or sum(item.byte_size for item in manifest_assets)
            != bound.manifest.total_bytes
            or any(expected.byte_size != fetched.byte_size
                   for expected, fetched in zip(
                       manifest_assets, fetched_assets))):
        raise ValueError(
            "acquisition authority manifest counts/sizes do not verify")
    replayed_coverage = assess_coverage(
        manifest_assets,
        target_spatial=bound.target_spatial,
        target_temporal=bound.target_temporal,
        halo=bound.halo,
        assembly_mode=source_schema.assembly_mode,
    )
    if replayed_coverage != bound.coverage:
        raise ValueError(
            "acquisition authority coverage proof does not replay")
    derived_descriptor = source_schema.derive_descriptor(
        bound.target_spatial, bound.target_temporal)
    if descriptor != derived_descriptor:
        raise ValueError(
            "fetched content descriptor is not derived from authoritative "
            "source schema and coverage")
    if derived_descriptor.descriptor_id != authority.descriptor_id:
        raise ValueError("acquisition authority names another descriptor")
    expected_receipt_id = strict_hash({
        "schema": "stage8r-fetch-receipt-identity-v1",
        "manifest_root": binding["manifest_root"],
        "source_id": binding["source_id"],
        "content_root": binding["content_root"],
        "assets": [item.to_dict() for item in fetched_assets],
        "total_bytes": sum(item.byte_size for item in fetched_assets),
    })
    if binding["receipt_id"] != expected_receipt_id:
        raise ValueError("fetched content receipt identity does not verify")
    profile = ExecutionProfile.from_dict(
        strict_copy(authority.execution_profile))
    profile.implementation.verify_current()
    if profile.implementation.operation_key != ACQUISITION_OPERATION_KEY:
        raise ValueError(
            "acquisition authority execution profile names another operation")
    return binding, derived_descriptor, profile


def acquisition_capability_id(content: FetchedContentBinding) -> str:
    """Readable identity for one exact post-fetch content binding."""
    if not isinstance(content, FetchedContentBinding):
        raise TypeError("acquisition capability identity requires fetched content")
    return f"acquire:{content.source_id}:{content.binding_id[:16]}"


def lower_fetched_content_to_capability(
    content: FetchedContentBinding,
    execution_profile: ExecutionProfile,
    *,
    cost_units: int,
    capability_version: str = "1.0.0",
    evidence_profile_id: str = "evidence:unknown",
) -> CapabilitySpec:
    """Turn verified exact bytes into a producer the selector can choose."""
    if not isinstance(content, FetchedContentBinding):
        raise TypeError(
            "acquisition lowering requires a verified FetchedContentBinding; "
            "a manifest or caller-authored descriptor is not authority")
    if not isinstance(execution_profile, ExecutionProfile):
        raise TypeError("lowering requires an ExecutionProfile")
    if isinstance(cost_units, bool) or not isinstance(cost_units, int) \
            or cost_units < 0:
        raise ValueError("acquisition cost_units must be a non-negative integer")
    if execution_profile.implementation.operation_key != ACQUISITION_OPERATION_KEY:
        raise ValueError(
            "acquisition capabilities must use the closed materialize operation")

    authority = AcquisitionAuthority._bind_verified_content(
        content,
        execution_profile,
        capability_version=capability_version,
        cost_units=cost_units,
        evidence_profile_id=evidence_profile_id,
        lowering_rule_implementation_sha256=
            _lowering_rule_implementation_sha256(),
    )

    return CapabilitySpec.bind(
        capability_id=acquisition_capability_id(content),
        capability_version=capability_version,
        implementation=execution_profile.implementation,
        binder=BinderRef.from_key(ACQUISITION_BINDER_KEY),
        input_ports=(),
        output_ports=(DescriptorTemplate("result", content.descriptor),),
        parameter_schema=ParameterSchema((
            ParameterField("content_binding", ParameterKind.JSON),
        )),
        parameterizations=(BindingParameterization(
            {"content_binding": content.to_dict()},
            {"cost_units": cost_units},
        ),),
        execution_profile_id=execution_profile.profile_id,
        evidence_profile_id=evidence_profile_id,
        acquisition_authority=authority,
    )


def verify_capability_acquisition_authority(
        capability: CapabilitySpec,
) -> dict[str, Any]:
    """Independently replay the exact reserved acquisition lowering."""
    if not isinstance(capability, CapabilitySpec):
        raise TypeError("capability must be CapabilitySpec")
    authority = capability.acquisition_authority
    if authority is None:
        raise ValueError("reserved acquisition capability lacks authority")
    binding, descriptor, profile = _verified_authority_contract(authority)
    expected_parameterizations = (BindingParameterization(
        {"content_binding": binding},
        {"cost_units": authority.cost_units},
    ),)
    comparisons = {
        "capability_id": (
            f"acquire:{binding['source_id']}:{binding['binding_id'][:16]}",
            capability.capability_id,
        ),
        "capability_version": (
            authority.capability_version, capability.capability_version),
        "implementation": (profile.implementation, capability.implementation),
        "binder": (
            BinderRef.from_key(ACQUISITION_BINDER_KEY), capability.binder),
        "input_ports": ((), capability.input_ports),
        "output_ports": (
            (DescriptorTemplate("result", descriptor),),
            capability.output_ports,
        ),
        "parameter_schema": (
            ParameterSchema((
                ParameterField("content_binding", ParameterKind.JSON),
            )),
            capability.parameter_schema,
        ),
        "parameterizations": (
            expected_parameterizations, capability.parameterizations),
        "applicability_key": ("always.v1", capability.applicability_key),
        "evidence_profile_id": (
            authority.evidence_profile_id, capability.evidence_profile_id),
        "cost_model_id": ("cost:declared-v1", capability.cost_model_id),
        "execution_profile_id": (
            profile.profile_id, capability.execution_profile_id),
        "acquisition_authority": (authority, capability.acquisition_authority),
    }
    mismatches = sorted(
        name for name, (expected, observed) in comparisons.items()
        if expected != observed)
    if mismatches:
        raise ValueError(
            "acquisition authority disagrees with capability lowering: "
            f"{mismatches}")
    return binding


def verify_bound_acquisition_authority(
        invocation: BoundInvocation,
) -> dict[str, Any]:
    """Independently replay authority retained by a bound invocation."""
    if not isinstance(invocation, BoundInvocation):
        raise TypeError("invocation must be BoundInvocation")
    authority = invocation.acquisition_authority
    if authority is None:
        raise ValueError("reserved acquisition invocation lacks authority")
    binding, descriptor, profile = _verified_authority_contract(authority)
    expected_outputs = (BoundOutputPort("result", descriptor),)
    expected_parameters = BindingParameterization(
        {"content_binding": binding}, {}).parameters
    comparisons = {
        "capability_id": (
            f"acquire:{binding['source_id']}:{binding['binding_id'][:16]}",
            invocation.capability_id,
        ),
        "capability_version": (
            authority.capability_version, invocation.capability_version),
        "implementation": (profile.implementation, invocation.implementation),
        "binder": (
            BinderRef.from_key(ACQUISITION_BINDER_KEY), invocation.binder),
        "parameters": (
            expected_parameters, invocation.parameters),
        "input_uses": ((), invocation.input_uses),
        "outputs": (expected_outputs, invocation.outputs),
        "metric_estimates": (
            {"cost_units": authority.cost_units}, invocation.metric_estimates),
        "evidence_profile_id": (
            authority.evidence_profile_id, invocation.evidence_profile_id),
        "cost_model_id": ("cost:declared-v1", invocation.cost_model_id),
        "execution_profile_id": (
            profile.profile_id, invocation.execution_profile_id),
        "acquisition_authority": (authority, invocation.acquisition_authority),
    }
    mismatches = sorted(
        name for name, (expected, observed) in comparisons.items()
        if expected != observed)
    if mismatches:
        raise ValueError(
            "acquisition authority disagrees with bound invocation: "
            f"{mismatches}")
    return binding


def lower_manifest_to_capability(content, *args, **kwargs) -> CapabilitySpec:
    """Compatibility name with a fail-closed Stage-8R authority check."""
    if not isinstance(content, FetchedContentBinding):
        raise TypeError(
            "manifest/descriptor lowering is forbidden; fetch and verify an "
            "exact FetchedContentBinding first")
    return lower_fetched_content_to_capability(content, *args, **kwargs)


__all__ = [
    "ACQUISITION_BINDER_KEY",
    "ACQUISITION_OPERATION_KEY",
    "acquisition_capability_id",
    "lower_fetched_content_to_capability",
    "lower_manifest_to_capability",
    "verify_bound_acquisition_authority",
    "verify_capability_acquisition_authority",
]
