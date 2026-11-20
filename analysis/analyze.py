"""Experiment CSV -> summary table and charts for the report.

Reads one or more CSVs written by scripts/run_experiment.py (one row per run),
aggregates every metric per scenario and load level as mean +- SD over the
repetitions, and writes into --out (default results/analysis/):

    summary.csv              mean, sd, n per scenario x level x metric
    summary.md               one Markdown table for the report
    cpu_services.png         CPU of the four services
    latency.png              p95 and p99 latency
    collector_network.png    network traffic into the collector
    collector_memory.png     collector RAM
    traces_kept.png          share of error and slow traces kept

SD is the sample SD (n - 1); with a single repetition it is left out.

    uv run analysis/analyze.py                            # results/raw/runs.csv
    uv run analysis/analyze.py results/raw/pilot.csv
"""

import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

SCENARIO_NAMES = {
    "A": "A: no tracing",
    "B": "B: head 100%",
    "C": "C: head 10%",
    "D": "D: tail",
    "E": "E: adaptive",
}
LEVEL_ORDER = ["low", "high"]

# metric -> (table header, decimals). Order = column order in summary.md.
METRICS = {
    "rps": ("RPS", 1),
    "p50_ms": ("p50, ms", 0),
    "p95_ms": ("p95, ms", 0),
    "p99_ms": ("p99, ms", 0),
    "cpu_services": ("CPU services, cores", 3),
    "cpu_collector": ("CPU collector, cores", 3),
    "mem_services_mb": ("RAM services, MiB", 1),
    "mem_collector_mb": ("RAM collector, MiB", 1),
    "collector_rx_kbps": ("Net to collector, KiB/s", 1),
    "spans_stored_per_s": ("Spans stored/s", 1),
    "errors_kept_pct": ("Errors kept, %", 1),
    "slow_kept_pct": ("Slow kept, %", 1),
}

# Reference categorical palette (dataviz skill), slots 1-2, light mode.
LEVEL_COLORS = {"low": "#2a78d6", "high": "#eb6834"}
TEXT = "#0b0b0b"
TEXT_MUTED = "#52514e"
GRID = "#e4e3df"


def load_runs(paths: list[Path]) -> pd.DataFrame:
    missing = [p for p in paths if not p.exists()]
    if missing:
        sys.exit(f"not found: {', '.join(map(str, missing))}")
    df = pd.concat([pd.read_csv(p) for p in paths], ignore_index=True)
    if dup := sorted(df.loc[df["run_id"].duplicated(), "run_id"]):
        sys.exit(f"duplicate run_id(s) across inputs: {dup}")

    df["collector_rx_kbps"] = df["collector_rx_bytes"] / df["duration_s"] / 1024
    df["spans_stored_per_s"] = df["spans_stored"] / df["duration_s"]
    df["errors_kept_pct"] = df["errors_kept_share"] * 100
    df["slow_kept_pct"] = df["slow_kept_share"] * 100
    return df


def aggregate(df: pd.DataFrame) -> pd.DataFrame:
    """Columns (metric, stat) with stat in mean/sd/n; index (scenario, level)."""
    agg = df.groupby(["scenario", "level"])[list(METRICS)].agg(["mean", "std", "count"])
    agg = agg.rename(columns={"std": "sd", "count": "n"}, level=1)
    levels = [lv for lv in LEVEL_ORDER if lv in agg.index.get_level_values("level")]
    levels += sorted(set(agg.index.get_level_values("level")) - set(levels))
    order = [(s, lv) for s in sorted(agg.index.get_level_values("scenario").unique()) for lv in levels]
    return agg.reindex([i for i in order if i in agg.index])


def write_summary_csv(agg: pd.DataFrame, path: Path) -> None:
    tidy = agg.stack(level=0, future_stack=True).reset_index()
    tidy = tidy.rename(columns={"level_2": "metric"})[["scenario", "level", "metric", "mean", "sd", "n"]]
    tidy.to_csv(path, index=False, float_format="%.6g")


def fmt(mean: float, sd: float, decimals: int) -> str:
    if pd.isna(mean):
        return "–"
    if pd.isna(sd):
        return f"{mean:.{decimals}f}"
    return f"{mean:.{decimals}f} ± {sd:.{decimals}f}"


def write_summary_md(agg: pd.DataFrame, path: Path) -> None:
    header = ["Scenario", "Load", "n"] + [h for h, _ in METRICS.values()]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * 3 + "---:|" * len(METRICS)]
    for (scenario, level), row in agg.iterrows():
        n = int(row[("rps", "n")])
        cells = [SCENARIO_NAMES.get(scenario, scenario), level, str(n)]
        cells += [fmt(row[(m, "mean")], row[(m, "sd")], d) for m, (_, d) in METRICS.items()]
        lines.append("| " + " | ".join(cells) + " |")
    note = (
        "\nValues are mean ± sample SD over the repetitions (n); with n = 1 only "
        "the value is shown. CPU in cores, RAM as working set; network is bytes "
        "received by the collector container; kept shares are traces of failed / "
        "slow requests found in Jaeger.\n"
    )
    path.write_text("\n".join(lines) + "\n" + note)


def bar_chart(agg: pd.DataFrame, panels: list[tuple[str, str]], ylabel: str, title: str, path: Path) -> None:
    """Grouped bars: x = scenario, one bar per load level, error bars = SD.

    panels: [(metric, panel title)]; several panels share the y-axis unit.
    """
    scenarios = list(agg.index.get_level_values("scenario").unique())
    levels = list(agg.index.get_level_values("level").unique())
    x = np.arange(len(scenarios))
    width = 0.8 / len(levels)

    fig, axes = plt.subplots(1, len(panels), figsize=(4.2 * len(panels) + 1.2, 3.8), sharey=True, squeeze=False)
    for ax, (metric, panel_title) in zip(axes[0], panels):
        for i, level in enumerate(levels):
            means = [agg[(metric, "mean")].get((s, level), np.nan) for s in scenarios]
            sds = np.array([agg[(metric, "sd")].get((s, level), np.nan) for s in scenarios])
            ax.bar(
                x + (i - (len(levels) - 1) / 2) * width,
                means,
                width,
                yerr=None if np.isnan(sds).all() else sds,  # no SD with n = 1
                label=f"{level} load",
                color=LEVEL_COLORS.get(level, "#6250d6"),
                edgecolor="white",
                linewidth=1.5,  # surface gap between adjacent bars
                error_kw={"ecolor": TEXT_MUTED, "elinewidth": 1, "capsize": 3},
                zorder=3,
            )
        ax.set_xticks(x, [SCENARIO_NAMES.get(s, s).replace(": ", "\n") for s in scenarios])
        if len(panels) > 1:
            ax.set_title(panel_title, fontsize=10, color=TEXT)
        ax.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
        ax.spines[["top", "right", "left"]].set_visible(False)
        ax.spines["bottom"].set_color(TEXT_MUTED)
        ax.tick_params(colors=TEXT_MUTED, length=0, labelsize=9)
    # After all panels are drawn: the shared y-axis must fit every panel.
    axes[0][0].set_ylim(bottom=0)
    axes[0][0].set_ylabel(ylabel, color=TEXT_MUTED)
    fig.suptitle(title, x=0.01, ha="left", fontsize=11, color=TEXT)
    # Legend in the title row, so it never covers a bar.
    fig.legend(*axes[0][0].get_legend_handles_labels(), loc="upper right", ncols=len(levels),
               frameon=False, fontsize=9, labelcolor=TEXT_MUTED)  # fmt: skip
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(path, dpi=150, facecolor="white")
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser(description="Summarise experiment CSV(s) into a table and charts.")
    ap.add_argument("csv", nargs="*", type=Path, default=[ROOT / "results" / "raw" / "runs.csv"])
    ap.add_argument("--out", type=Path, default=ROOT / "results" / "analysis")
    args = ap.parse_args()

    df = load_runs(args.csv)
    agg = aggregate(df)
    args.out.mkdir(parents=True, exist_ok=True)

    write_summary_csv(agg, args.out / "summary.csv")
    write_summary_md(agg, args.out / "summary.md")
    charts = [
        ([("cpu_services", "")], "CPU, cores", "Service CPU (gateway + orders + payments + inventory)",
         "cpu_services.png"),
        ([("p95_ms", "p95"), ("p99_ms", "p99")], "Latency, ms", "Request latency", "latency.png"),
        ([("collector_rx_kbps", "")], "KiB/s", "Network traffic into the collector", "collector_network.png"),
        ([("mem_collector_mb", "")], "RAM, MiB", "Collector memory (working set)", "collector_memory.png"),
        ([("errors_kept_pct", "Error traces"), ("slow_kept_pct", "Slow traces (≥ 500 ms)")], "Kept, %",
         "Share of interesting traces kept", "traces_kept.png"),
    ]  # fmt: skip
    for panels, ylabel, title, name in charts:
        bar_chart(agg, panels, ylabel, title, args.out / name)

    runs = ", ".join(f"{s}/{lv}: {int(n)}" for (s, lv), n in agg[("rps", "n")].items())
    print(f"{len(df)} runs ({runs}) -> {args.out}")


if __name__ == "__main__":
    main()
