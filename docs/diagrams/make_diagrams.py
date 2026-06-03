#!/usr/bin/env python3
"""Generate architecture diagrams (PNG) for the design document.

Pure matplotlib so it runs anywhere matplotlib is installed:

    <python-with-matplotlib> docs/diagrams/make_diagrams.py

Writes:
    docs/diagrams/01_cascade.png
    docs/diagrams/02_abstraction.png
    docs/diagrams/03_walkthrough.png
    docs/diagrams/04_resolve.png
    docs/diagrams/05_layers.png
"""
from __future__ import annotations

import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- palette -------------------------------------------------------------
BG      = "#0e1116"
INK     = "#e6edf3"
MUTED   = "#9aa7b2"
IMPACT  = ("#3b0a0a", "#ff6b6b")
MODEL   = ("#0b2545", "#4da6ff")
MODEL2  = ("#2a1a3a", "#c79bff")
DATA    = ("#13262f", "#5cdbb5")
GOAL    = ("#3b2a0a", "#ffd166")
GOOD    = ("#0b2e1a", "#5cdb95")
BAD     = ("#3b0a0a", "#ff6b6b")
NEUTRAL = ("#161b22", "#30363d")


def _fig(w, h):
    fig, ax = plt.subplots(figsize=(w, h))
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis("off")
    return fig, ax


def box(ax, x, y, w, h, text, fill, edge, *, fs=11, bold=False,
        text_color=INK, round=0.02):
    p = FancyBboxPatch(
        (x, y), w, h,
        boxstyle=f"round,pad=0.4,rounding_size={round*100}",
        linewidth=2, facecolor=fill, edgecolor=edge, zorder=2)
    ax.add_patch(p)
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            color=text_color, fontsize=fs,
            fontweight="bold" if bold else "normal", zorder=3,
            wrap=True)
    return (x + w / 2, y + h / 2)


def arrow(ax, p0, p1, *, color=MUTED, label="", style="-|>",
          lw=2.0, ls="-", rad=0.0, fs=9, label_dy=2.2):
    a = FancyArrowPatch(
        p0, p1, arrowstyle=style, mutation_scale=16,
        linewidth=lw, color=color, linestyle=ls,
        connectionstyle=f"arc3,rad={rad}", zorder=1,
        shrinkA=6, shrinkB=6)
    ax.add_patch(a)
    if label:
        mx, my = (p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2
        ax.text(mx, my + label_dy, label, ha="center", va="center",
                color=MUTED, fontsize=fs, style="italic", zorder=4)


def title(ax, t, sub=""):
    ax.text(50, 96, t, ha="center", va="top", color=INK,
            fontsize=15, fontweight="bold")
    if sub:
        ax.text(50, 90.5, sub, ha="center", va="top", color=MUTED,
                fontsize=10.5)


def save(fig, name):
    out = os.path.join(HERE, name)
    fig.savefig(out, dpi=160, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print("wrote", out)


# =========================================================================
# 1. Cascade of consequences
# =========================================================================
def diagram_cascade():
    fig, ax = _fig(11, 7)
    title(ax, "One impact, a cascade of consequences",
          "each arrow = one model's output is the next model's input")

    impact = box(ax, 38, 80, 24, 9, "ASTEROID IMPACT", *IMPACT,
                 bold=True, fs=12)
    th = box(ax, 8,  64, 22, 8, "Thermal pulse", *DATA, fs=10)
    bl = box(ax, 39, 64, 22, 8, "Blast / seismic", *DATA, fs=10)
    ej = box(ax, 70, 64, 22, 8, "Ejecta / dust", *DATA, fs=10)
    arrow(ax, impact, (th[0], 72)); arrow(ax, impact, (bl[0], 72))
    arrow(ax, impact, (ej[0], 72))

    fire  = box(ax, 8,  46, 22, 9, "WILDFIRE\n(model)", *MODEL, bold=True)
    smoke = box(ax, 39, 46, 22, 9, "SMOKE /\nAEROSOLS (model)", *MODEL, bold=True)
    air   = box(ax, 70, 46, 22, 9, "AIR QUALITY\n(model)", *MODEL, bold=True)
    arrow(ax, th, fire)
    arrow(ax, fire, smoke, label="smoke emitted", color=MODEL[1])
    arrow(ax, smoke, air, label="transport", color=MODEL[1])

    flood = box(ax, 8,  28, 22, 9, "FLOODING\n(model)", *MODEL, bold=True)
    health= box(ax, 70, 28, 22, 9, "HEALTH /\nEXPOSURE", *DATA)
    arrow(ax, fire, flood, label="burn scars", color=MODEL[1], rad=-0.2)
    arrow(ax, air, health)

    ax.text(50, 12,
            "The science lives in separate, mature models.\n"
            "We make the cascade itself a pluggable, reproducible object.",
            ha="center", va="center", color=INK, fontsize=11,
            bbox=dict(boxstyle="round,pad=0.6", facecolor=NEUTRAL[0],
                      edgecolor=NEUTRAL[1]))
    save(fig, "01_cascade.png")


# =========================================================================
# 2. The producer abstraction + resolver
# =========================================================================
def diagram_abstraction():
    fig, ax = _fig(11, 7.5)
    title(ax, "The abstraction: everything is a producer",
          "one contract — produces / requires / run — for data sources AND models")

    # contract banner
    box(ax, 22, 77, 56, 8,
        "ONE CONTRACT:   produces  ·  requires  ·  run()",
        *MODEL, bold=True, fs=12)

    drv = box(ax, 8, 62, 34, 12,
              "DATA DRIVER\nrequires: (none)\nproduces: wind", *DATA, fs=10)
    mod = box(ax, 58, 62, 34, 12,
              "MODEL\nrequires: fuel, wind, ignition\nproduces: fire_arrival",
              *MODEL, fs=10)
    ax.text(50, 68, "the engine\ncannot tell\nthem apart", ha="center",
            va="center", color=MUTED, fontsize=9, style="italic")

    # resolver
    ask = box(ax, 36, 50, 28, 7, "ask for a target", *NEUTRAL, fs=10)
    res = box(ax, 30, 35, 40, 9,
              "RESOLVE\nin catalog, fresh & fine enough?", *GOAL, fs=10, bold=True)
    use = box(ax, 8, 35, 18, 9, "use it", *GOOD, fs=10)
    findrun = box(ax, 74, 35, 20, 9, "find producer\n→ run it", *MODEL2, fs=9)

    arrow(ax, drv, (ask[0]-6, 57), color=DATA[1], rad=0.1)
    arrow(ax, mod, (ask[0]+6, 57), color=MODEL[1], rad=-0.1)
    arrow(ax, ask, res)
    arrow(ax, res, use, label="yes", color=GOOD[1])
    arrow(ax, res, findrun, label="no", color=MODEL2[1])
    arrow(ax, findrun, (res[0]+20, 39), color=MODEL2[1], rad=-0.4,
          ls="--", label="")
    ax.annotate("", xy=(70, 41), xytext=(84, 41),
                arrowprops=dict(arrowstyle="-|>", color=MODEL2[1], lw=2,
                                connectionstyle="arc3,rad=0.5"))

    cat = box(ax, 16, 17, 30, 10,
              "DATA CATALOG\nsingle source of truth\nnative-res + lineage",
              "#2a1a3a", "#c79bff", fs=10, bold=True)
    infra = box(ax, 54, 17, 30, 10,
                "INFRASTRUCTURE\nsame code, any machine\n(a separate concern)",
                *NEUTRAL, fs=9)
    arrow(ax, use, (cat[0]-4, 27), color=MUTED, rad=0.2)
    arrow(ax, findrun, (cat[0]+8, 27), color=MUTED, rad=-0.2)
    arrow(ax, findrun, (infra[0], 27), color=MUTED, rad=0.0)
    save(fig, "02_abstraction.png")


# =========================================================================
# 3. WRF-SFIRE + smoke_model walkthrough
# =========================================================================
def diagram_walkthrough():
    fig, ax = _fig(12, 7)
    title(ax, "Walkthrough: WRF-SFIRE (model 1) → smoke_model (model 2)",
          "user asks only for 'smoke_concentration'; the engine discovers the chain")

    kml  = box(ax, 4, 74, 20, 8, "thermal pulse\n(KML)", *DATA, fs=9)
    fuel = box(ax, 4, 60, 20, 8, "fuel map", *DATA, fs=9)
    wind = box(ax, 4, 46, 20, 8, "ERA5 wind", *DATA, fs=9)
    dem  = box(ax, 4, 32, 20, 8, "terrain DEM", *DATA, fs=9)

    wrf = box(ax, 36, 48, 24, 18,
              "WRF-SFIRE\n★ MODEL 1\nfire spread +\natmosphere",
              *MODEL, bold=True, fs=11)
    arrow(ax, kml,  (36, 60), label="ignition", color=DATA[1])
    arrow(ax, fuel, (36, 56), label="fuel", color=DATA[1])
    arrow(ax, wind, (36, 52), label="wind", color=DATA[1])
    arrow(ax, dem,  (36, 48), label="terrain", color=DATA[1])

    smoke = box(ax, 72, 48, 24, 18,
                "smoke_model\n★ MODEL 2\n(hypothetical)\nplume + chem",
                *MODEL2, bold=True, fs=11)
    arrow(ax, wrf, (72, 60), label="fire_arrival", color=MODEL[1])
    arrow(ax, wrf, (72, 52), label="burned_area", color=MODEL[1])
    arrow(ax, wind, (72, 46), label="wind reused (cached)",
          color=DATA[1], ls="--", rad=-0.35)

    goal = box(ax, 74, 26, 22, 9, "GOAL\nsmoke_concentration", *GOAL,
               bold=True, fs=10)
    arrow(ax, smoke, goal, color=GOAL[1])

    ax.text(50, 12,
            "Add model 2 = declare its contract (requires/produces) + register one line.\n"
            "The engine wires the cascade. No engine code changes.",
            ha="center", va="center", color=INK, fontsize=10.5,
            bbox=dict(boxstyle="round,pad=0.6", facecolor=NEUTRAL[0],
                      edgecolor=NEUTRAL[1]))
    save(fig, "03_walkthrough.png")


# =========================================================================
# 4. The central challenge: reuse vs recompute
# =========================================================================
def diagram_resolve():
    fig, ax = _fig(9.5, 8)
    title(ax, "The central challenge: 'is it already in the system?'",
          "before an expensive model runs — REUSE or RECOMPUTE?")

    req = box(ax, 34, 84, 32, 7, "request a variable", *NEUTRAL, fs=10)
    qs = [
        ("covers THIS time window?", 72),
        ("FINE-ENOUGH resolution?", 60),
        ("inputs CURRENT (not stale)?", 48),
        ("provenance / config matches?", 36),
    ]
    prev = req
    qcenters = []
    for text, y in qs:
        c = box(ax, 30, y, 40, 8, text, *GOAL, fs=10)
        qcenters.append((c, y))
        arrow(ax, prev, (c[0], y + 8))
        prev = c

    rc = box(ax, 76, 46, 20, 12,
             "RECOMPUTE\n+ cascade\ninvalidation", *BAD, bold=True, fs=10)
    for c, y in qcenters:
        arrow(ax, (70, y + 4), (76, 52), label="no", color=BAD[1],
              rad=-0.15, fs=8, label_dy=1.2)

    reuse = box(ax, 30, 22, 40, 8, "✔ REUSE  (skip the expensive run)",
                *GOOD, bold=True, fs=11)
    arrow(ax, prev, reuse, label="all yes", color=GOOD[1])

    ax.text(50, 11,
            "Wrong one way → stale / too-coarse data (bad science).\n"
            "Wrong the other → recompute a 50,000-core run needlessly.\n"
            "Satisfaction is multi-dimensional: time × resolution × freshness × provenance.",
            ha="center", va="center", color=INK, fontsize=10,
            bbox=dict(boxstyle="round,pad=0.6", facecolor=NEUTRAL[0],
                      edgecolor=NEUTRAL[1]))
    save(fig, "04_resolve.png")


# =========================================================================
# 5. Layered architecture
# =========================================================================
def diagram_layers():
    fig, ax = _fig(11, 7.5)
    title(ax, "Layered architecture",
          "the engine, catalog, and infrastructure never change per model")

    box(ax, 10, 80, 80, 9,
        "USER   —   scenario config (extent, nests, days, smoke)  ·  ask for targets",
        *GOAL, fs=11, bold=True)

    box(ax, 10, 62, 80, 12,
        "ORCHESTRATION ENGINE  (model-agnostic)\n"
        "build dependency DAG from targets  ·  resolve each need  ·  execute",
        *MODEL, fs=11, bold=True)

    box(ax, 10, 42, 38, 14,
        "DATA CATALOG\nZarr arrays + index\nevery var @ native res\n+ lineage / provenance",
        "#2a1a3a", "#c79bff", fs=10, bold=True)
    box(ax, 52, 42, 38, 14,
        "PRODUCERS (plug-ins)\ndata drivers  +  models\nWRF-SFIRE = model 1\nsmoke / flood = next",
        *DATA, fs=10, bold=True)

    box(ax, 10, 24, 80, 9,
        "INFRASTRUCTURE INDEPENDENCE  (a supporting concern, not the focus)\n"
        "same run on a laptop or a supercomputer",
        *NEUTRAL, fs=10)

    # connecting arrows
    arrow(ax, (50, 80), (50, 74))
    arrow(ax, (50, 62), (29, 56))
    arrow(ax, (50, 62), (71, 56))
    arrow(ax, (71, 42), (50, 33), color=MUTED)
    ax.text(50, 15,
            "Adding a model never touches the engine, catalog, or other models.",
            ha="center", va="center", color=INK, fontsize=11,
            bbox=dict(boxstyle="round,pad=0.5", facecolor=NEUTRAL[0],
                      edgecolor=NEUTRAL[1]))
    save(fig, "05_layers.png")


if __name__ == "__main__":
    diagram_cascade()
    diagram_abstraction()
    diagram_walkthrough()
    diagram_resolve()
    diagram_layers()
    print("done")
