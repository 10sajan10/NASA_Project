"""Magnus-Tetens saturation vapor pressure and humidity utilities.

These provide a thermodynamically-consistent way to express RH at any hour
when only a daily mean dewpoint and a diurnal temperature swing are known.

References
----------
Alduchov, O. A., & Eskridge, R. E. (1996). Improved Magnus form approximation
of saturation vapor pressure. Journal of Applied Meteorology, 35(4), 601-609.

Lawrence, M. G. (2005). The relationship between relative humidity and the
dewpoint temperature in moist air. BAMS, 86(2), 225-234.
"""
from __future__ import annotations

import numpy as np

# Magnus-Tetens coefficients (Alduchov & Eskridge 1996, "Magnus form")
A = 17.625
B = 243.04


def saturation_vapor_pressure_hPa(t_c: np.ndarray) -> np.ndarray:
    """Saturation vapour pressure over liquid water [hPa] for T in C."""
    return 6.1094 * np.exp(A * t_c / (B + t_c))


def relative_humidity_pct(t_c: np.ndarray, td_c: np.ndarray) -> np.ndarray:
    """Relative humidity [%] given air temperature and dewpoint, both Celsius."""
    es_t = saturation_vapor_pressure_hPa(t_c)
    es_d = saturation_vapor_pressure_hPa(td_c)
    rh = 100.0 * es_d / np.maximum(es_t, 1e-9)
    return np.clip(rh, 0.0, 100.0)


def dewpoint_from_rh(t_c: np.ndarray, rh_pct: np.ndarray) -> np.ndarray:
    """Dewpoint [C] from temperature [C] and RH [%]."""
    rh = np.clip(rh_pct, 1e-3, 100.0) / 100.0
    gamma = np.log(rh) + A * t_c / (B + t_c)
    return B * gamma / (A - gamma)


def diurnal_temperature(t_max_c: np.ndarray, t_min_c: np.ndarray,
                        hour: float | int | np.ndarray,
                        peak_hour: float = 14.0) -> np.ndarray:
    """Sinusoidal diurnal temperature.

    T(h) = (T_max + T_min)/2 + (T_max - T_min)/2 * cos(2π(h - peak)/24)

    Reaches T_max at peak_hour (default 14:00 LST), T_min at peak_hour - 12.
    Used to disaggregate a daily mean / extremes into hourly values without a
    separate hourly weather model.
    """
    mid = 0.5 * (t_max_c + t_min_c)
    amp = 0.5 * (t_max_c - t_min_c)
    phase = np.cos(2.0 * np.pi * (hour - peak_hour) / 24.0)
    return mid + amp * phase
