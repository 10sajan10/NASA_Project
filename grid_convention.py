"""Dependency-neutral identity for the canonical scientific field grid."""

# Kept outside ``contracts`` and ``engine.runtime`` so both layers can use the
# same identifier without creating an import cycle during runtime bootstrap.
GRID_AFFINE_CONVENTION = "sample-centres-axis-aligned-v1"
FIELD_JSON_SCHEMA = "field-json-v2"
FIELD_JSON_VALIDATOR_KIND = "field_json_v2"
LEGACY_FIELD_JSON_SCHEMA = "field-json-v1"

__all__ = [
    "FIELD_JSON_SCHEMA",
    "FIELD_JSON_VALIDATOR_KIND",
    "GRID_AFFINE_CONVENTION",
    "LEGACY_FIELD_JSON_SCHEMA",
]
