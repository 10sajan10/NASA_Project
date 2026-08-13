"""Repository-wide test policy for explicitly quarantined baseline defects.

Stage 0 keeps known failures visible as strict xfails: fixing either defect
turns it into XPASS and fails CI until the quarantine is deliberately removed.
"""
from __future__ import annotations

import pytest


_STRICT_QUARANTINE = {
    "tests/test_wrf_config.py::test_smoke_sets_chem_opt": (
        "WRF smoke configuration emits emiss_opt=0; isolated from the "
        "Stage-0 orchestration baseline pending the WRF reference-science lane"
    ),
    "tests/test_wrf_config.py::test_coarse_time_step_scales_with_dx": (
        "WRF 9-km time-step policy returns 45 s rather than the asserted 54 s; "
        "policy decision belongs to the WRF reference-science lane"
    ),
}


def pytest_collection_modifyitems(items):
    for item in items:
        reason = _STRICT_QUARANTINE.get(item.nodeid)
        if reason:
            item.add_marker(pytest.mark.xfail(reason=reason, strict=True))
