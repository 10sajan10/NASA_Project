"""WRF-SFIRE provisioning agent — system-agnostic, self-contained builds.

Probe the host, decide between a container build (preferred for portability)
or a from-source build that brings its own dependency stack, and execute it.
Nothing here depends on a host's pre-built libraries or module system.
"""
from .system_probe import SystemProbe
from .agent import WRFSFireProvisioningAgent, Plan, Step

__all__ = ["SystemProbe", "WRFSFireProvisioningAgent", "Plan", "Step"]
