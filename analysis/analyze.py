"""Experiment CSV -> summary tables and charts for the report.

Reads one or more CSVs written by scripts/run_experiment.py (one row per run),
aggregates every metric per scenario and load level as mean +- SD and a 95%
confidence interval of the mean over the repetitions, and writes into --out
(default results/analysis/):

    summary.csv              mean, sd, n, ci_low, ci_high per scenario x level x metric
    summary.md               mean +- SD table and 95% CI table for the report
    cpu_services.png         CPU of the four services
    latency.png              p50 and p99 latency
    collector_network.png    network traffic into the collector
    collector_memory.png     collector RAM
    traces_kept.png          share of error and slow traces kept

With --lang uk the charts get Ukrainian labels and go to <out>/uk/ instead;
the summary files are the same for both languages.

SD is the sample SD (n - 1). The CI is mean +- t(0.975, n - 1) * SD / sqrt(n)
(Student's t). With a single repetition SD and CI are left out.

    uv run analysis/analyze.py                            # results/raw/runs.csv
    uv run analysis/analyze.py --lang uk                  # Ukrainian charts
    uv run analysis/analyze.py results/raw/pilot.csv
"""

import argparse
import sys
import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy import stats  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CI_LEVEL = 0.95

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
# Key metrics for the CI table in summary.md.
CI_METRICS = [
    "cpu_services",
    "p50_ms",
    "p99_ms",
    "collector_rx_kbps",
    "spans_stored_per_s",
    "mem_collector_mb",
    "errors_kept_pct",
    "slow_kept_pct",
]

# Chart labels per language. Strategy terms (head, tail-based) stay in English.
LABELS = {
    "en": {
        "scenarios": {
            "A": "A: no tracing",
            "B": "B: head 100%",
            "C": "C: head 10%",
            "D": "D: tail",
            "E": "E: adaptive",
        },
        "levels": {"low": "low load", "high": "high load"},
        "subtitle": "Mean over runs (n = {n}); error bars = SD",
        "charts": {
            "cpu_services": ("Service CPU (gateway + orders + payments + inventory)", "CPU, cores", {}),
            "latency": ("Request latency", "Latency, ms", {"p50_ms": "p50", "p99_ms": "p99"}),
            "collector_network": ("Network traffic into the collector", "KiB/s", {}),
            "collector_memory": ("Collector memory (working set)", "RAM, MiB", {}),
            "traces_kept": (
                "Share of interesting traces kept",
                "Kept, %",
                {"errors_kept_pct": "Error traces", "slow_kept_pct": "Slow traces (≥ 500 ms)"},
            ),
        },
    },
    "uk": {
        "scenarios": {
            "A": "A: без трасування",
            "B": "B: head 100%",
            "C": "C: head 10%",
            "D": "D: tail-based",
            "E": "E: адаптивний",
        },
        "levels": {"low": "низьке навантаження", "high": "високе навантаження"},
        "subtitle": "Середнє за запусками (n = {n}); планки похибок = SD",
        "charts": {
            "cpu_services": ("CPU сервісів (gateway + orders + payments + inventory)", "CPU, ядра CPU", {}),
            "latency": ("Затримка запитів", "Затримка, мс", {"p50_ms": "p50", "p99_ms": "p99"}),
            "collector_network": ("Мережевий трафік до колектора", "Трафік, КіБ/с", {}),
            "collector_memory": ("Пам’ять колектора (робочий набір)", "RAM, МіБ", {}),
            "traces_kept": (
                "Частка збережених цікавих трас",
                "Збережено, %",
                {"errors_kept_pct": "Траси з помилками", "slow_kept_pct": "Повільні траси (≥ 500 мс)"},
            ),
        },
    },
}
# chart name -> metrics drawn (one panel per metric).
CHARTS = {
    "cpu_services": ["cpu_services"],
    "latency": ["p50_ms", "p99_ms"],
    "collector_network": ["collector_rx_kbps"],
    "collector_memory": ["mem_collector_mb"],
    "traces_kept": ["errors_kept_pct", "slow_kept_pct"],
}
# Charts whose panels get their own y-axis: p99 is ~4x p50, so on a shared
# axis the p50 differences between scenarios would be hard to see.
OWN_Y_AXIS = {"latency"}

# Reference categorical palette (dataviz skill), slots 1-2, light mode.
LEVEL_COLORS = {"low": "#2a78d6", "high": "#eb6834"}
TEXT = "#0b0b0b"
TEXT_MUTED = "#52514e"
GRID = "#e4e3df"
# DejaVu Sans ships with matplotlib and covers Cyrillic.
FONT = "DejaVu Sans"


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
    """Columns (metric, stat) with stat in mean/sd/n/ci_low/ci_high; index (scenario, level)."""
    agg = df.groupby(["scenario", "level"])[list(METRICS)].agg(["mean", "std", "count"])
    agg = agg.rename(columns={"std": "sd", "count": "n"}, level=1)
    for m in METRICS:
        n = agg[(m, "n")]
        # t(0.975, n - 1); NaN for n = 1, so the CI is left out as well.
        t = pd.Series(stats.t.ppf(0.5 + CI_LEVEL / 2, n.where(n > 1) - 1), index=agg.index)
        half = t * agg[(m, "sd")] / np.sqrt(n)
        agg[(m, "ci_low")] = agg[(m, "mean")] - half
        agg[(m, "ci_high")] = agg[(m, "mean")] + half
    agg = agg[[(m, s) for m in METRICS for s in ["mean", "sd", "n", "ci_low", "ci_high"]]]

    levels = [lv for lv in LEVEL_ORDER if lv in agg.index.get_level_values("level")]
    levels += sorted(set(agg.index.get_level_values("level")) - set(levels))
    order = [(s, lv) for s in sorted(agg.index.get_level_values("scenario").unique()) for lv in levels]
    return agg.reindex([i for i in order if i in agg.index])


def write_summary_csv(agg: pd.DataFrame, path: Path) -> None:
    tidy = agg.stack(level=0, future_stack=True).reset_index()
    tidy = tidy.rename(columns={"level_2": "metric"})
    tidy = tidy[["scenario", "level", "metric", "mean", "sd", "n", "ci_low", "ci_high"]]
    tidy.to_csv(path, index=False, float_format="%.6g")


def fmt(mean: float, sd: float, decimals: int) -> str:
    if pd.isna(mean):
        return "–"
    if pd.isna(sd):
        return f"{mean:.{decimals}f}"
    return f"{mean:.{decimals}f} ± {sd:.{decimals}f}"


def fmt_ci(mean: float, low: float, high: float, decimals: int) -> str:
    if pd.isna(mean):
        return "–"
    if pd.isna(low):
        return f"{mean:.{decimals}f}"
    return f"{mean:.{decimals}f} [{low:.{decimals}f}, {high:.{decimals}f}]"


def md_table(header: list[str], rows: list[list[str]], left: int) -> list[str]:
    align = "|" + "---|" * left + "---:|" * (len(header) - left)
    return ["| " + " | ".join(header) + " |", align] + ["| " + " | ".join(r) + " |" for r in rows]


def write_summary_md(agg: pd.DataFrame, path: Path) -> None:
    names = LABELS["en"]["scenarios"]
    sd_rows, ci_rows = [], []
    for (scenario, level), row in agg.iterrows():
        n = int(row[("rps", "n")])
        ident = [names.get(scenario, scenario), level, str(n)]
        sd_rows.append(ident + [fmt(row[(m, "mean")], row[(m, "sd")], d) for m, (_, d) in METRICS.items()])
        ci_rows.append(
            ident
            + [
                fmt_ci(row[(m, "mean")], row[(m, "ci_low")], row[(m, "ci_high")], METRICS[m][1])
                for m in CI_METRICS
            ]
        )
    ident_header = ["Scenario", "Load", "n"]
    pct = round(CI_LEVEL * 100)
    lines = ["## Mean ± SD", ""]
    lines += md_table(ident_header + [h for h, _ in METRICS.values()], sd_rows, left=3)
    lines += [
        "",
        "Values are mean ± sample SD over the repetitions (n); with n = 1 only "
        "the value is shown. CPU in cores, RAM as working set; network is bytes "
        "received by the collector container; kept shares are traces of failed / "
        "slow requests found in Jaeger.",
        "",
        f"## Mean and {pct}% confidence interval",
        "",
    ]
    lines += md_table(ident_header + [METRICS[m][0] for m in CI_METRICS], ci_rows, left=3)
    lines += [
        "",
        f"Values are mean [{pct}% CI of the mean], CI = mean ± t({0.5 + CI_LEVEL / 2:.3f}, n − 1) · SD / √n "
        f"(Student's t; t = {stats.t.ppf(0.5 + CI_LEVEL / 2, 2):.3f} for n = 3). "
        "Non-overlapping intervals mark a clear difference; with n = 3 the intervals "
        "are wide, so differences of a few percent cannot be detected.",
        "",
    ]
    path.write_text("\n".join(lines))


def tick_label(name: str) -> str:
    """'A: no tracing' -> 'A' over the description, long descriptions wrapped."""
    key, _, desc = name.partition(": ")
    return "\n".join([key, *textwrap.wrap(desc, 11)]) if desc else key


def bar_chart(agg: pd.DataFrame, chart: str, lang: str, path: Path) -> None:
    """Grouped bars: x = scenario, one bar per load level, error bars = SD.

    Several panels share the y-axis unit, and the axis itself unless the chart
    is in OWN_Y_AXIS. Layout is fixed in inches: a header
    band for title + subtitle, a footer band for the legend, axes in between,
    so the legend never overlaps the title, the ticks or a bar.
    """
    labels = LABELS[lang]
    title, ylabel, panel_titles = labels["charts"][chart]
    metrics = CHARTS[chart]
    scenarios = list(agg.index.get_level_values("scenario").unique())
    levels = list(agg.index.get_level_values("level").unique())
    x = np.arange(len(scenarios))
    width = 0.8 / len(levels)
    n_runs = agg[(metrics[0], "n")]
    n_text = str(int(n_runs.iloc[0])) if n_runs.nunique() == 1 else f"{int(n_runs.min())}–{int(n_runs.max())}"

    width_in = 4.4 * len(metrics) + 1.2
    height_in, header_in, footer_in = 4.4, 0.75, 0.45
    shared_y = chart not in OWN_Y_AXIS
    fig, axes = plt.subplots(1, len(metrics), figsize=(width_in, height_in), sharey=shared_y, squeeze=False)
    for ax, metric in zip(axes[0], metrics):
        for i, level in enumerate(levels):
            means = [agg[(metric, "mean")].get((s, level), np.nan) for s in scenarios]
            sds = np.array([agg[(metric, "sd")].get((s, level), np.nan) for s in scenarios])
            ax.bar(
                x + (i - (len(levels) - 1) / 2) * width,
                means,
                width,
                yerr=None if np.isnan(sds).all() else sds,  # no SD with n = 1
                label=labels["levels"].get(level, level),
                color=LEVEL_COLORS.get(level, "#6250d6"),
                edgecolor="white",
                linewidth=1.5,  # surface gap between adjacent bars
                error_kw={"ecolor": TEXT_MUTED, "elinewidth": 1, "capsize": 3},
                zorder=3,
            )
        ax.set_xticks(x, [tick_label(labels["scenarios"].get(s, s)) for s in scenarios])
        if len(metrics) > 1:
            ax.set_title(panel_titles[metric], fontsize=10, color=TEXT)
        ax.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
        ax.spines[["top", "right", "left"]].set_visible(False)
        ax.spines["bottom"].set_color(TEXT_MUTED)
        ax.tick_params(colors=TEXT_MUTED, length=0, labelsize=9)
    # After all panels are drawn: a shared y-axis must fit every panel.
    for ax in axes[0] if not shared_y else axes[0][:1]:
        ax.set_ylim(bottom=0)
    axes[0][0].set_ylabel(ylabel, color=TEXT_MUTED)

    pad_x = 0.12 / width_in
    fig.text(pad_x, 1 - 0.12 / height_in, title, ha="left", va="top", fontsize=11, color=TEXT)
    fig.text(pad_x, 1 - 0.42 / height_in, labels["subtitle"].format(n=n_text), ha="left", va="top",
             fontsize=9, color=TEXT_MUTED)  # fmt: skip
    fig.legend(*axes[0][0].get_legend_handles_labels(), loc="lower center", bbox_to_anchor=(0.5, 0.06 / height_in),
               ncols=len(levels), frameon=False, fontsize=9, labelcolor=TEXT_MUTED)  # fmt: skip
    fig.tight_layout(rect=(0, footer_in / height_in, 1, 1 - header_in / height_in))
    fig.savefig(path, dpi=150, facecolor="white")
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser(description="Summarise experiment CSV(s) into tables and charts.")
    ap.add_argument("csv", nargs="*", type=Path, default=[ROOT / "results" / "raw" / "runs.csv"])
    ap.add_argument("--out", type=Path, default=ROOT / "results" / "analysis")
    ap.add_argument("--lang", choices=sorted(LABELS), default="en",
                    help="chart language; uk writes the charts to <out>/uk/")  # fmt: skip
    args = ap.parse_args()

    df = load_runs(args.csv)
    agg = aggregate(df)
    chart_dir = args.out if args.lang == "en" else args.out / args.lang
    chart_dir.mkdir(parents=True, exist_ok=True)

    write_summary_csv(agg, args.out / "summary.csv")
    write_summary_md(agg, args.out / "summary.md")
    plt.rcParams["font.family"] = FONT
    for chart in CHARTS:
        bar_chart(agg, chart, args.lang, chart_dir / f"{chart}.png")

    runs = ", ".join(f"{s}/{lv}: {int(n)}" for (s, lv), n in agg[("rps", "n")].items())
    print(f"{len(df)} runs ({runs}) -> {args.out} (charts: {chart_dir})")


if __name__ == "__main__":
    main()
