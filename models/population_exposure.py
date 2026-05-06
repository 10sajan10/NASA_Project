"""Population exposure model."""
from __future__ import annotations

import numpy as np

from cube.store import Cube
from fusion.producers import BaseProducer, VariableRequest


class PopulationExposureProducer(BaseProducer):
    name = "population_exposure"
    produces = ["population_affected"]
    requires = ["population", "arrival_s"]
    kind = "model"
    can_run_parallel = False

    def run(self, cube: Cube, request: VariableRequest) -> list[str]:
        pop = cube.read_static("population").astype(np.float32)
        arrival = cube.read_static("arrival_s")
        affected = np.where(arrival >= 0, pop, 0.0).astype(np.float32)
        total = float(np.nansum(pop))
        exposed = float(np.nansum(affected))
        cube.write_static(
            "population_affected", affected,
            source=f"arrival_s overlay; exposed={exposed:.0f}; total={total:.0f}",
            native_res_m=cube.grid.pixel_m, units="people",
            producer=self.name,
            description="Population count in cells reached by simulated fire")
        print(f"      population exposed: {exposed:,.0f} / {total:,.0f}")
        return ["population_affected"]
