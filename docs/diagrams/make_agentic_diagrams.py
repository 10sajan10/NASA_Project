#!/usr/bin/env python3
"""Slide-ready (16:9) agentic-architecture diagrams.

Same pure-matplotlib style as make_diagrams.py, sized for a PowerPoint
slide (13.33 x 7.5 in — drop the PNG onto a slide, no resizing):

    <python-with-matplotlib> docs/diagrams/make_agentic_diagrams.py

Writes:
    docs/diagrams/06_agentic_architecture.png
    docs/diagrams/07_agent_workflow.png
    docs/diagrams/08_asteroid_cascade.png
"""
from __future__ import annotations

import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch

HERE = os.path.dirname(os.path.abspath(__file__))

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


def _fig():
    fig, ax = plt.subplots(figsize=(13.33, 7.5))   # 16:9 PowerPoint
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 100)
    ax.axis("off")
    return fig, ax


def box(ax, x, y, w, h, text, colors, *, fs=10, bold=False, lw=2):
    fill, edge = colors
    p = FancyBboxPatch((x, y), w, h,
                       boxstyle="round,pad=0.4,rounding_size=1.5",
                       linewidth=lw, facecolor=fill, edgecolor=edge,
                       zorder=2)
    ax.add_patch(p)
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center",
            color=INK, fontsize=fs,
            fontweight="bold" if bold else "normal", zorder=3)
    return (x + w / 2, y + h / 2)


def band(ax, x, y, w, h, label, edge, *, fs=10):
    p = FancyBboxPatch((x, y), w, h,
                       boxstyle="round,pad=0.4,rounding_size=1.5",
                       linewidth=1.4, facecolor="none", edgecolor=edge,
                       linestyle=(0, (4, 3)), zorder=1)
    ax.add_patch(p)
    ax.text(x + 1.2, y + h - 0.6, label, ha="left", va="top",
            color=edge, fontsize=fs, fontweight="bold", zorder=3)


def arrow(ax, p0, p1, *, color=MUTED, label="", lw=2.0, ls="-",
          rad=0.0, fs=8.5, dx=0.0, dy=2.0):
    a = FancyArrowPatch(p0, p1, arrowstyle="-|>", mutation_scale=15,
                        linewidth=lw, color=color, linestyle=ls,
                        connectionstyle=f"arc3,rad={rad}", zorder=1,
                        shrinkA=7, shrinkB=7)
    ax.add_patch(a)
    if label:
        mx, my = (p0[0] + p1[0]) / 2 + dx, (p0[1] + p1[1]) / 2 + dy
        ax.text(mx, my, label, ha="center", va="center", color=color,
                fontsize=fs, style="italic", zorder=4)


def title(ax, t, sub=""):
    ax.text(50, 99.5, t, ha="center", va="top", color=INK,
            fontsize=17, fontweight="bold")
    if sub:
        ax.text(50, 94.6, sub, ha="center", va="top", color=MUTED,
                fontsize=10.5)


def save(fig, name):
    out = os.path.join(HERE, name)
    fig.savefig(out, dpi=160, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print("wrote", out)


# =========================================================================
# 06. Overall architecture
# =========================================================================
def diagram_architecture():
    fig, ax = _fig()
    title(ax, "Agentic cascade architecture",
          "the LLM proposes (selection + binding) - the deterministic "
          "engine disposes (validation + execution)")

    # entry points -----------------------------------------------------
    band(ax, 2, 80, 96, 11, "ENTRY POINTS", MUTED)
    e1 = box(ax, 4, 81.5, 21, 7, "Question\nask_cascade.py", NEUTRAL, fs=9.5)
    e2 = box(ax, 27, 81.5, 21, 7, "Event JSON\nevents/inbox + watcher",
             NEUTRAL, fs=9.5)
    e3 = box(ax, 50, 81.5, 21, 7, "MCP client\n(Claude Code)", NEUTRAL,
             fs=9.5)
    e4 = box(ax, 73, 81.5, 23, 7, "Human CLI\nrun_cascade.py", NEUTRAL,
             fs=9.5)

    # agentic layer ------------------------------------------------------
    band(ax, 2, 47, 96, 30, "AGENTIC LAYER (agentic/)  -  decides WHAT and WHICH",
         GOAL[1])
    ag = box(ax, 4, 63, 20, 9,
             "PlannerAgent\nClaude tool-use loop\nrevises on PlanError",
             MODEL2, fs=9.5, bold=True)
    ts = box(ax, 27, 63, 24, 9,
             "Tool surface\nsearch - resolve_plan\nestimate - submit_plan",
             MODEL2, fs=9.5)
    pl = box(ax, 27, 49.5, 24, 10.5,
             "Deterministic planner\nintent - targets - backward chain\n"
             "filter + score + resolution",
             GOAL, fs=9.5, bold=True)
    mc = box(ax, 54, 49.5, 20, 10.5,
             "MetaCatalog (DuckDB)\ncards: coverage, provenance,\n"
             "trust, regimes, cost + ontology",
             DATA, fs=9)
    ex = box(ax, 4, 49.5, 20, 10.5,
             "Executor\nRunPlan -> run_cascade\n(--dry-run default)",
             NEUTRAL, fs=9.5)
    cr = box(ax, 77, 49.5, 19, 22.5,
             "Critic\npresence\ncoverage >= 60%\nsanity bounds\n|\n"
             "exclude failed\nproducer -> replan",
             BAD, fs=9.5)

    # engine -------------------------------------------------------------
    band(ax, 2, 23.5, 74, 20.5, "ENGINE (engine/)  -  decides HOW, executes",
         MODEL[1])
    cat = box(ax, 4, 25.5, 20, 13,
              "CascadeCatalog\nbuild_registry(\n  only=plan bindings)",
              MODEL, fs=9.5)
    dag = box(ax, 27, 25.5, 21, 13,
              "Pipeline.from_targets\nbackward-chained DAG",
              MODEL, fs=9.5)
    run = box(ax, 51, 25.5, 23, 13,
              "PipelineRunner\nserial/process/dask/SLURM\nretries - tiles - "
              "dirty prop", MODEL, fs=9.5)

    # producers + cube -----------------------------------------------------
    pr = box(ax, 77, 26.5, 19, 11,
             "PRODUCERS\ndrivers + models\n(ProducerV2)", MODEL2, fs=9.5)
    cb = box(ax, 24, 4, 52, 12,
             "CUBE  -  Zarr per variable + DuckDB catalog v2\n"
             "provenance: source_url - license - checksum - run_id\n"
             "resolution-aware satisfies() - lineage",
             DATA, fs=10, bold=True)

    # arrows ---------------------------------------------------------------
    arrow(ax, e1, ag)
    arrow(ax, e2, ts)
    arrow(ax, e3, ts)
    arrow(ax, (ag[0] + 10, ag[1]), (ts[0] - 12, ts[1]))
    arrow(ax, ts, pl)
    arrow(ax, (pl[0] + 12, pl[1]), (mc[0] - 10, mc[1]))
    arrow(ax, (pl[0] - 12, pl[1]), (ex[0] + 10, ex[1]),
          label="validated RunPlan", dy=3.0, color=GOAL[1])
    arrow(ax, ex, (e4[0] - 20, 49), rad=-0.4)
    arrow(ax, e4, (cat[0], 40))
    arrow(ax, (cat[0] + 10, cat[1]), (dag[0] - 10, dag[1]))
    arrow(ax, (dag[0] + 10, dag[1]), (run[0] - 11, run[1]))
    arrow(ax, (run[0] + 11, run[1]), (pr[0] - 9, pr[1]))
    arrow(ax, pr, (cb[0] + 22, 16), label="write variables", dx=8, dy=1.5)
    arrow(ax, (cb[0] - 15, 16), (run[0] - 5, 25),
          label="satisfies() / skip", dx=-11, dy=0)
    arrow(ax, (cb[0] + 26, cb[1] + 4), (cr[0], 48), color=BAD[1], rad=-0.25)
    arrow(ax, (cr[0], cr[1] + 12), (pl[0] + 12, pl[1] + 4),
          label="replan(exclude)", color=BAD[1], rad=-0.25, dy=4.5)

    save(fig, "06_agentic_architecture.png")


# =========================================================================
# 07. Agent workflow
# =========================================================================
def diagram_workflow():
    fig, ax = _fig()
    title(ax, "Agent workflow - resolver in the loop",
          "the accepted plan is always planner output; PlanError is the "
          "anti-hallucination feedback")

    y1, y2, h = 66, 38, 15
    s1 = box(ax, 3, y1, 16, h, "1  Question / event\n->  EventSpec\n+ intent",
             NEUTRAL, fs=9.5)
    s2 = box(ax, 22.5, y1, 17, h,
             "2  Search catalog\nontology, datasets,\nmodels (filtered)",
             DATA, fs=9.5)
    s3 = box(ax, 43, y1, 18, h,
             "3  resolve_plan\ndry-run through the\ndeterministic planner",
             GOAL, fs=9.5, bold=True)
    s4 = box(ax, 64.5, y1, 15, h, "4  estimate_cost\nresolution vs\ncores table",
             DATA, fs=9.5)
    s5 = box(ax, 82.5, y1, 15, h, "5  submit_plan\nre-validated\n-> RunPlan",
             GOAL, fs=9.5, bold=True)

    s6 = box(ax, 82.5, y2, 15, h, "6  Execute\nrun_cascade\ndry-run -> real",
             MODEL, fs=9.5)
    s7 = box(ax, 61, y2, 17, h, "7  Engine DAG\nonly bound producers\nrun; cube skips",
             MODEL, fs=9.5)
    s8 = box(ax, 39.5, y2, 17, h, "8  Critic\npresence, coverage,\nsanity bounds",
             BAD, fs=9.5)
    s9 = box(ax, 3, y2, 32, h,
             "9  Verified report\nplan + bindings + alternatives\n"
             "+ rationale + lineage", GOOD, fs=9.5, bold=True)

    arrow(ax, (s1[0] + 8, s1[1]), (s2[0] - 8.5, s2[1]))
    arrow(ax, (s2[0] + 8.5, s2[1]), (s3[0] - 9, s3[1]))
    arrow(ax, (s3[0] + 9, s3[1]), (s4[0] - 7.5, s4[1]))
    arrow(ax, (s4[0] + 7.5, s4[1]), (s5[0] - 7.5, s5[1]))
    arrow(ax, (s5[0], s5[1] - 7.5), (s6[0], s6[1] + 7.5))
    arrow(ax, (s6[0] - 7.5, s6[1]), (s7[0] + 8.5, s7[1]))
    arrow(ax, (s7[0] - 8.5, s7[1]), (s8[0] + 8.5, s8[1]))
    arrow(ax, (s8[0] - 8.5, s8[1]), (s9[0] + 16, s9[1]), color=GOOD[1],
          label="clean", dy=2.4)

    # feedback loops
    arrow(ax, (s3[0], s3[1] + 7.5), (s2[0], s2[1] + 7.5), color=BAD[1],
          rad=0.35,
          label="PlanError: variable + chain -> revise", dy=8.5, fs=9)
    arrow(ax, (s8[0], s8[1] - 7.5), (s6[0], s6[1] - 7.5), color=BAD[1],
          rad=0.35,
          label="findings -> replan(exclude) -> re-execute", dy=-8.5, fs=9)

    ax.text(50, 10.5,
            "LLM decides: intent, targets, arbitration among near-ties, "
            "budget.   Deterministic code decides: candidate filtering, "
            "scoring, resolution, DAG, execution, verification.",
            ha="center", color=MUTED, fontsize=10, style="italic")

    save(fig, "07_agent_workflow.png")


# =========================================================================
# 08. Asteroid cascade example
# =========================================================================
def diagram_asteroid():
    fig, ax = _fig()
    title(ax, "Asteroid event -> cascading impact",
          "backward-chaining = focus: intent economic builds ONLY the "
          "consequence branch; the atmospheric chain is never constructed")

    evt = box(ax, 2, 55, 15, 18,
              "EVENT\nasteroid_impact\nDallas - 5 Mt\nr = 50 km",
              IMPACT, fs=10, bold=True)

    # consequence branch (top)
    band(ax, 20, 55, 60, 33,
         "CONSEQUENCE BRANCH - intent=economic (30 m, seconds)", GOAL[1])
    imp = box(ax, 22, 58, 16, 12,
              "impact_scaling\nCollins laws\n(scaling-law)", MODEL, fs=9)
    dmg = box(ax, 42, 58, 16, 12,
              "blast_damage\nlogistic curve\np50 = 35 kPa", MODEL, fs=9)
    eco = box(ax, 62, 58, 16, 12,
              "econ_loss\ndamage x assets\nx 1.45 indirect", MODEL, fs=9)
    exp = box(ax, 42, 74.5, 16, 9, "exposure\npopulation + assets",
              DATA, fs=9)

    arrow(ax, (imp[0] + 8, imp[1]), (dmg[0] - 8, dmg[1]),
          label="overpressure_pa", dy=-3.2)
    arrow(ax, (dmg[0] + 8, dmg[1]), (eco[0] - 8, eco[1]),
          label="damage_frac", dy=-3.2)
    arrow(ax, (exp[0] + 8, exp[1]), (eco[0], eco[1] + 6), rad=-0.2,
          label="assets, population", dx=8, dy=2.0)

    # atmospheric branch (bottom)
    band(ax, 20, 14, 60, 33,
         "ATMOSPHERIC BRANCH - intent=fire / air_quality (100 m+, hours)",
         MODEL2[1])
    th = box(ax, 22, 34, 16, 8, "thermal (KML)\nignition_t0", DATA, fs=8.5)
    lf = box(ax, 22, 25, 16, 8, "landfire\nnfuel_cat", DATA, fs=8.5)
    dm = box(ax, 22, 16, 16, 8, "dem + era5_wind", DATA, fs=8.5)
    wrf = box(ax, 45, 20.5, 17, 17,
              "wrf_sfire\n(+chem)\nfull physics\nMPI", MODEL2, fs=9.5,
              bold=True)
    out2 = box(ax, 66, 20.5, 12, 17, "arrival_s\nfire_area\npm25", NEUTRAL,
               fs=8.5)
    arrow(ax, (th[0] + 8, th[1]), (wrf[0] - 9, wrf[1] + 5))
    arrow(ax, (lf[0] + 8, lf[1]), (wrf[0] - 9, wrf[1]))
    arrow(ax, (dm[0] + 8, dm[1]), (wrf[0] - 9, wrf[1] - 5))
    arrow(ax, (wrf[0] + 9, wrf[1]), (out2[0] - 6.5, out2[1]))

    arrow(ax, (evt[0] + 5, evt[1] + 5), (imp[0] - 8, imp[1]),
          color=GOAL[1], label="economic", dy=3.0)
    arrow(ax, (evt[0] + 5, evt[1] - 5), (th[0] - 8, wrf[1]),
          color=MODEL2[1], ls=(0, (4, 3)), label="full_cascade only",
          dx=-2, dy=-3.0)

    # cube + critic + report (right column)
    cube = box(ax, 84, 55, 14, 18,
               "CUBE\nloss_usd\nexposure\n+ lineage", DATA, fs=9.5,
               bold=True)
    crit = box(ax, 84, 33, 14, 15, "CRITIC\ndamage in [0,1]\nloss >= 0\n"
               "coverage ok?", BAD, fs=9)
    rep = box(ax, 84, 14, 14, 12, "REPORT\nplan + lineage\n+ alternatives",
              GOOD, fs=9, bold=True)

    arrow(ax, (eco[0] + 8, eco[1]), (cube[0] - 7, cube[1]),
          label="loss_usd", dx=-3.5, dy=-3.2)
    arrow(ax, (out2[0] + 6, out2[1]), (cube[0] - 3, 54), rad=-0.25)
    arrow(ax, (cube[0], cube[1] - 9), (crit[0], crit[1] + 7.5))
    arrow(ax, (crit[0], crit[1] - 7.5), (rep[0], rep[1] + 6),
          color=GOOD[1], label="ok", dx=3)
    arrow(ax, (crit[0] - 7, crit[1]), (eco[0], eco[1] - 6.5),
          color=BAD[1], rad=0.3, ls=(0, (4, 3)))
    ax.text(79, 53.5, "fail -> exclude + replan", ha="center",
            va="center", color=BAD[1], fontsize=8.5, style="italic",
            zorder=4)

    save(fig, "08_asteroid_cascade.png")


if __name__ == "__main__":
    diagram_architecture()
    diagram_workflow()
    diagram_asteroid()
