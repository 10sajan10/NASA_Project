"""Vectorised Rothermel (1972) / Albini (1976) surface fire spread.

The published Rothermel coefficients are calibrated for U.S. customary
units (sigma in ft^-1, load in lb/ft^2, depth in ft, heat in BTU/lb,
wind in ft/min). Our fuel table and the cube use SI throughout, so the
function converts inputs to US units, evaluates the algorithm there,
then converts R back to m/min for the cube.

Inputs (all 2-D arrays, same shape):
  fuel       : dict from fuel_models.fuel_arrays(codes)
  m_1h..m_lw : moisture content fractions (0..1)
  U_mid      : 10-m wind reduced to midflame, m/s
  slope_tan  : tan(slope_deg)
Returns:
  R_head [m/min], LB [-]      (zeros / ones in non-burnable cells)
"""
from __future__ import annotations
import numpy as np


# Particle density (oven-dry wood)
RHO_P_LB_FT3 = 32.0          # lb/ft^3 (Rothermel constant)

# Mineral fractions
S_T = 0.0555                 # total mineral
S_E = 0.01                   # effective (silica-free) mineral

# Unit conversions
KG_M2_TO_LB_FT2 = 0.20481614
M_TO_FT         = 1.0 / 0.3048
FT_INV_FROM_M_INV = 0.3048   # m^-1 -> ft^-1
KJ_KG_TO_BTU_LB = 1.0 / 2.326
MS_TO_FT_MIN    = 196.8504


def rothermel_R(
    fuel: dict[str, np.ndarray],
    m_1h: np.ndarray, m_10h: np.ndarray, m_100h: np.ndarray,
    m_lh: np.ndarray, m_lw: np.ndarray,
    U_mid: np.ndarray, slope_tan: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    # ---- pull SI inputs and convert to US ----
    w1   = fuel["load_1h"]   * KG_M2_TO_LB_FT2
    w10  = fuel["load_10h"]  * KG_M2_TO_LB_FT2
    w100 = fuel["load_100h"] * KG_M2_TO_LB_FT2
    wlh  = fuel["load_lh"]   * KG_M2_TO_LB_FT2
    wlw  = fuel["load_lw"]   * KG_M2_TO_LB_FT2
    s1   = fuel["sav_1h"]   * FT_INV_FROM_M_INV
    slh  = fuel["sav_lh"]   * FT_INV_FROM_M_INV
    slw  = fuel["sav_lw"]   * FT_INV_FROM_M_INV
    delta = np.maximum(fuel["depth_m"] * M_TO_FT, 1e-3)
    Mx_d = np.maximum(fuel["mx_dead"], 0.05)
    h    = fuel["heat_kJkg"] * KJ_KG_TO_BTU_LB        # BTU/lb
    burnable = fuel["burnable"]
    dynamic  = fuel["dynamic"]
    U_ftpm   = U_mid * MS_TO_FT_MIN

    # ---- dynamic herb shift (Scott & Burgan) ----
    f_dead = np.clip((1.20 - m_lh) / (1.20 - 0.30), 0.0, 1.0)
    f_dead = np.where(dynamic, f_dead, 0.0)
    w1_eff  = w1 + f_dead * wlh
    wlh_eff = (1 - f_dead) * wlh

    # ---- net loads (subtract total mineral) ----
    wn1   = w1_eff   * (1 - S_T)
    wn10  = w10      * (1 - S_T)
    wn100 = w100     * (1 - S_T)
    wnlh  = wlh_eff  * (1 - S_T)
    wnlw  = wlw      * (1 - S_T)

    Wd  = wn1 + wn10 + wn100
    Wl  = wnlh + wnlw
    W   = Wd + Wl

    safe = burnable & (W > 1e-6) & (Wd > 1e-6)
    R_out  = np.zeros_like(W, dtype=np.float32)
    LB_out = np.ones_like(W, dtype=np.float32)
    if not np.any(safe):
        return R_out, LB_out

    # ---- characteristic SAV (load-weighted, US units) ----
    sav_d = np.where(Wd > 0, s1, 0.0)
    sav_l = np.where(wnlh > 0, slh, slw)
    sigma = np.where(W > 0, (Wd * sav_d + Wl * sav_l) / np.maximum(W, 1e-12), 1500.0)
    sigma = np.clip(sigma, 100.0, 4000.0)         # ft^-1, guard

    # bulk density in lb/ft^3, packing ratio
    rho_b = W / delta                              # lb/ft^3
    beta  = rho_b / RHO_P_LB_FT3
    beta_op = 3.348 * sigma ** -0.8189
    rb = beta / np.maximum(beta_op, 1e-9)
    rb = np.clip(rb, 1e-3, 5.0)                    # avoid extreme exp blowup

    # reaction velocity (Rothermel 1972)
    A = 133.0 * sigma ** -0.7913
    Gamma_max = sigma ** 1.5 / (495.0 + 0.0594 * sigma ** 1.5)
    Gamma     = Gamma_max * rb ** A * np.exp(A * (1 - rb))   # 1/min

    # moisture damping (dead and live separately)
    M_d = np.where(Wd > 0,
                   (wn1 * m_1h + wn10 * m_10h + wn100 * m_100h)
                   / np.maximum(Wd, 1e-12),
                   0.0)
    M_l = np.where(Wl > 0,
                   (wnlh * m_lh + wnlw * m_lw) / np.maximum(Wl, 1e-12),
                   0.0)
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

    eta_s = 0.174 * S_E ** -0.19                  # mineral damping (constant ~0.42)

    # reaction intensity, BTU/ft^2/min
    I_R = Gamma * (Wd * h * eta_M_d + Wl * h * eta_M_l) * eta_s

    # propagating flux ratio
    xi = np.exp((0.792 + 0.681 * np.sqrt(sigma)) * (beta + 0.1)) / \
         (192.0 + 0.2595 * sigma)

    # heat sink
    eps = np.exp(-138.0 / sigma)
    M_f = M_d
    Q_ig = 250.0 + 1116.0 * M_f                   # BTU/lb

    # wind / slope coefficients (Albini 1976)
    B = 0.02526 * sigma ** 0.54
    C_ = 7.47 * np.exp(-0.133 * sigma ** 0.55)
    E_ = 0.715 * np.exp(-3.59e-4 * sigma)
    # Rothermel/BehavePlus effective-wind limit: U_lim = 0.9 * I_R (ft/min),
    # an empirical bound above which the wind term over-extrapolates.
    U_lim = 0.9 * I_R
    U_eff = np.minimum(np.maximum(U_ftpm, 0.0),
                       np.maximum(U_lim, 1.0))
    phi_w = C_ * np.power(U_eff, B) * np.power(np.maximum(rb, 1e-6), -E_)
    phi_s = 5.275 * np.power(np.maximum(beta, 1e-6), -0.3) * (slope_tan ** 2)

    # rate of spread, ft/min
    R_us = (I_R * xi * (1 + phi_w + phi_s)) / np.maximum(rho_b * eps * Q_ig, 1e-9)
    R_mpm = R_us * 0.3048                         # ft/min -> m/min

    # length-to-breadth (Anderson 1983), needs U in mi/h
    U_mph = U_mid * 2.23694
    LB_v = 0.936 * np.exp(0.2566 * U_mph) + 0.461 * np.exp(-0.1548 * U_mph) - 0.397
    LB_v = np.clip(LB_v, 1.0, 8.0)

    R_out  = np.where(safe, R_mpm, 0.0).astype(np.float32)
    LB_out = np.where(safe & (R_out > 0), LB_v, 1.0).astype(np.float32)
    return R_out, LB_out
