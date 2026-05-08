"""Fire-model adapter boundary.

Fire models plug into the resolver through this module. Each adapter declares
the cube variables it requires and produces, so the dependency resolver only
materializes the upstream data needed by the selected fire model.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from cube.store import Cube
from fusion.producers import BaseProducer, VariableRequest
from models import fire_spread


class FireModelAdapter(Protocol):
    """Contract implemented by pluggable fire models."""

    name: str
    produces: list[str]
    requires: list[str]

    def run(self, cube: Cube, request: VariableRequest) -> list[str]:
        ...


def _require_start(request: VariableRequest, name: str) -> datetime:
    if request.t_start is None:
        raise ValueError(f"{name} requires t_start")
    return request.t_start


class RothermelFireAdapter:
    """Adapter for the current Rothermel + Dijkstra fire-spread model."""

    name = "rothermel"
    produces = [
        "ignition_effective_t0", "R_head", "LB",
        "fireline_intensity_kw_m", "arrival_s", "fire",
    ]
    requires = [
        "fbfm40", "dem", "slope_deg", "aspect_deg",
        "burnable", "thermal_fluence", "lfmc_pct",
        "wind_speed_ms", "wind_dir_deg", "rh", "temp_c",
        "dfm_1hr", "dfm_10hr", "dfm_100hr", "kbdi",
        "hard_barrier", "urban_mask", "surface_spread_class",
        "ignition_threshold_mj_m2", "spread_threshold_kw_m",
        "spread_rate_modifier",
    ]

    def run(self, cube: Cube, request: VariableRequest) -> list[str]:
        out = fire_spread.run(
            cube, _require_start(request, self.name), request.n_days)
        return list(out.keys())


class FireModelProducer(BaseProducer):
    """Resolver producer wrapping the selected fire-model adapter."""

    name = "fire_model"
    kind = "model"
    can_run_parallel = False

    def __init__(self, adapter: FireModelAdapter):
        self.adapter = adapter
        self.produces = list(adapter.produces)
        self.requires = list(adapter.requires)

    def run(self, cube: Cube, request: VariableRequest) -> list[str]:
        return self.adapter.run(cube, request)


_FIRE_MODELS: dict[str, FireModelAdapter] = {}


def register_fire_model_adapter(adapter: FireModelAdapter, *,
                                replace: bool = False) -> FireModelAdapter:
    if adapter.name in _FIRE_MODELS and not replace:
        raise ValueError(f"fire model already registered: {adapter.name}")
    _FIRE_MODELS[adapter.name] = adapter
    return adapter


def available_fire_models() -> list[str]:
    return sorted(_FIRE_MODELS)


def fire_model_outputs(name: str) -> list[str]:
    return list(get_fire_model_adapter(name).produces)


def get_fire_model_adapter(name: str) -> FireModelAdapter:
    try:
        return _FIRE_MODELS[name]
    except KeyError as exc:
        choices = ", ".join(available_fire_models()) or "none"
        raise ValueError(f"unknown fire model {name!r}; choices: {choices}") from exc


def build_fire_model_producer(name: str) -> FireModelProducer:
    return FireModelProducer(get_fire_model_adapter(name))


register_fire_model_adapter(RothermelFireAdapter())
