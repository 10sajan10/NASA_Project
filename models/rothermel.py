"""Vectorised Rothermel (1972) / Albini (1976) surface fire spread.

All math is done in SI throughout. The single-cell scalar form is:

    R = (I_R * ξ * (1 + φ_w + φ_s)) / (ρ_b * ε * Q_ig)         [m/min]

Inputs are 2-D arrays from the cube (one value per simulation cell).
The output is the heading rate of spread (R_head) and the length-to-breadth
ratio LB (Anderson 1983) for the elliptical anisotropy used downstream.

Conventions
-----------
- M_*  : moisture content fractions (e.g. 0.08 = 8 %)
- σ    : SAV [m^-1]
- δ    : fuel bed depth [m]
- w    : oven-dry load [kg/m^2]
- ρ_p  : particle density = 512 kg/m^3 (oven-dry wood)
- U_mid: midflame wind speed [m/s]
- slope_tan: tan(slope_deg)
"""
from __future__ import annotations
import numpy as np


RHO_P = 512.0          # kg/m^3
S_T = 0.0555           # total mineral fraction
S_E = 0.01             # effective mineral fraction


def rothermel_R(
    fuel: dict[str, np.ndarray],
    m_1h: np.ndarray,
    m_10h: np.ndarray,
    m_100h: np.ndarray,
    m_lh: np.ndarray,
    m_lw: np.ndarray,
    U_mid: np.ndarray,
    slope_tan: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Return (R_head [m/min], LB [-]) per cell. Non-burnable cells are 0/1."""
    # ---- pull arrays
    w1, w10, w100 = fuel["load_1h"], fuel["load_10h"], fuel["load_100h"]
    wlh, wlw = fuel["load_lh"], fuel["load_lw"]
    s1, slh, slw = fuel["sav_1h"], fuel["sav_lh"], fuel["sav_lw"]
    delta = np.maximum(fuel["depth_m"], 1e-6)
    Mx_d = np.maximum(fuel["mx_dead"], 0.05)
    h = fuel["heat_kJkg"] * 1e3                 # J/kg
    burnable = fuel["burnable"]
    dynamic = fuel["dynamic"]

    # ---- dynamic herb shift: when LFMC drops below 30 %, herb load moves
    # progressively to dead-1h (Scott & Burgan 2005)
    lfmc = m_lh.copy()              # already in fraction (0..3)
    f_dead = np.clip((1.20 - lfmc) / (1.20 - 0.30), 0.0, 1.0)  # 0 at 120%, 1 at 30%
    f_dead = np.where(dynamic, f_dead, 0.0)
    w1_eff = w1 + f_dead * wlh
    wlh_eff = (1 - f_dead) * wlh
    sav_dead_1h = s1                 # SAV doesn't change much under shift

    # ---- net loads (subtract total mineral)
    wn1   = w1_eff   * (1 - S_T)
    wn10  = w10      * (1 - S_T)
    wn100 = w100     * (1 - S_T)
    wnlh  = wlh_eff  * (1 - S_T)
    wnlw  = wlw      * (1 - S_T)

    # weighted dead/live fuel SAV and load
    Wd  = wn1 + wn10 + wn100
    Wl  = wnlh + wnlw
    W   = Wd + Wl

    # protect against zero-fuel cells
    safe = burnable & (W > 1e-6) & (Wd > 1e-6)
    R = np.zeros_like(W, dtype=np.float32)
    LB = np.ones_like(W, dtype=np.float32)
    if not safe.any():
        return R, LB

    # weighted SAV (use the 1-h SAV for the dead bin; live uses LH SAV when present, else LW)
    sav_d = np.where(Wd > 0, sav_dead_1h, 0.0)
    sav_l = np.where(wnlh > 0, slh, slw)
    # bulk packing
    rho_b = W / delta
    beta  = rho_b / RHO_P
    sigma = (Wd * sav_d + Wl * sav_l) / np.maximum(W, 1e-12)
    sigma = np.maximum(sigma, 1.0)      # guard

    # optimum packing ratio + ratio
    beta_op = 3.348 * sigma ** -0.8189
    rb = beta / np.maximum(beta_op, 1e-12)

    # reaction velocity
    A = 133.0 * sigma ** -0.7913
    Gamma_max = sigma ** 1.5 / (495.0 + 0.0594 * sigma ** 1.5)
    Gamma     = Gamma_max * rb ** A * np.exp(A * (1 - rb))   # min^-1

    # moisture damping (separate for dead/live)
    M_d = np.where(Wd > 0,
                   (wn1 * m_1h + wn10 * m_10h + wn100 * m_100h)
                   / np.maximum(Wd, 1e-12), 0.0)
    M_l = np.where(Wl > 0,
                   (wnlh * m_lh + wnlw * m_lw) / np.maximum(Wl, 1e-12), 0.0)
    rM_d = np.minimum(M_d / np.maximum(Mx_d, 1e-6), 1.0)
    eta_M_d = 1 - 2.59*rM_d + 5.11*rM_d**2 - 3.52*rM_d**3
    eta_M_d = np.clip(eta_M_d, 0.0, 1.0)
    # live extinction moisture (Albini)
    Mx_l = np.where(
        Wl > 0,
        2.9 * (Wd / np.maximum(Wl, 1e-12)) * (1 - M_d / np.maximum(Mx_d, 1e-6)) - 0.226,
        0.05,
    )
    Mx_l = np.maximum(Mx_l, Mx_d)
    rM_l = np.minimum(M_l / np.maximum(Mx_l, 1e-6), 1.0)
    eta_M_l = 1 - 2.59*rM_l + 5.11*rM_l**2 - 3.52*rM_l**3
    eta_M_l = np.clip(eta_M_l, 0.0, 1.0)

    eta_s = 0.174 * S_E ** -0.19         # mineral damping (constant for s_e=0.01)

    # reaction intensity (split dead vs live)
    I_R = (Gamma * (Wd * h * eta_M_d + Wl * h * eta_M_l) * eta_s)  # J/m^2/min

    # propagating flux ratio
    xi = np.exp((0.792 + 0.681 * np.sqrt(sigma)) * (beta + 0.1)) / \
         (192.0 + 0.2595 * sigma)

    # heat sink
    eps = np.exp(-138.0 / sigma)
    M_f = np.where(Wd > 0, M_d, 0.0)
    Q_ig = (250.0 + 1116.0 * M_f) * 1e3            # J/kg

    # --- wind / slope coefficients (Albini 1976; uses U in ft/min) ---
    U_ftpm = U_mid * 196.85                        # m/s -> ft/min
    B = 0.02526 * sigma ** 0.54
    C = 7.47 * np.exp(-0.133 * sigma ** 0.55)
    E = 0.715 * np.exp(-3.59e-4 * sigma)
    phi_w = C * np.power(np.maximum(U_ftpm, 0.0), B) * \
            np.power(np.maximum(rb, 1e-6), -E)
    phi_s = 5.275 * np.power(np.maximum(beta, 1e-6), -0.3) * (slope_tan ** 2)

    # rate of spread [m/min]
    R_safe = (I_R * xi * (1 + phi_w + phi_s)) / np.maximum(rho_b * eps * Q_ig, 1e-9)
    R = np.where(safe, R_safe, 0.0).astype(np.float32)

    # length-to-breadth (Anderson 1983), needs U in mi/h
    U_mph = U_mid * 2.23694
    LB_v = 0.936 * np.exp(0.2566 * U_mph) + 0.461 * np.exp(-0.1548 * U_mph) - 0.397
    LB_v = np.clip(LB_v, 1.0, 8.0)
    LB = np.where(safe, LB_v, 1.0).astype(np.float32)

    return R, LB
