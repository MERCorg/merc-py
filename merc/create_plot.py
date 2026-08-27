import argparse
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Callable, Sequence

# Reuse the shared primitives that already power create_table.py. Both scripts
# live in the same directory, so the import works when this module is run as a
# script (its directory is on sys.path) or imported by a sibling.
from .create_table import (
    AGGREGATORS,
    Cell,
    load_records,
    sanitize_str,
)

# ---------------------------------------------------------------------------
# Generic scatter-plot interface
#
# This module is the plotting counterpart of create_table.py. The primitives
# below are reusable building blocks for assembling pgfplots/TikZ scatter plots
# from benchmark records. They are meant to be imported and "instantiated" by
# benchmark-specific scripts (for example create_plot_exploration.py), which
# describe their own series. The output is dependency-free LaTeX, just like the
# tables, so it compiles with a stock TeX distribution and no Python plotting
# libraries are required.
# ---------------------------------------------------------------------------

# Human-readable axis labels for the metrics a Cell can report.
METRIC_LABELS: dict[str, str] = {
    "time": "Time (s)",
    "memory": "Memory (MB)",
}

# A small palette/marker cycle so multiple series stay visually distinct.
DEFAULT_COLORS = ["blue", "red", "teal", "orange", "violet", "brown", "black"]
DEFAULT_MARKS = ["*", "square*", "triangle*", "diamond*", "pentagon*", "x", "+"]


@dataclass
class Point:
    """A single rendered data point: its coordinates and the case it came from."""

    x: float
    y: float
    label: str


@dataclass
class Series:
    """A single-source scatter series.

    Records matched by ``select`` are grouped by case (the benchmark ``name``)
    so every case contributes one aggregated point. The point's x and y come
    from the same matched records (for example time on the x-axis and memory on
    the y-axis).
    """

    label: str
    select: Callable[[dict], bool]
    x_metric: str = "time"
    y_metric: str = "memory"
    mark: str = "*"
    color: str = "blue"


@dataclass
class PlotSeries:
    """A fully resolved series ready to render: a legend label and its points."""

    label: str
    points: list[Point] = field(default_factory=list)
    mark: str = "*"
    color: str = "blue"


def build_points(
    records: Sequence[dict],
    select: Callable[[dict], bool],
    x_metric: str,
    y_metric: str,
    aggregate: Callable[[Sequence[float]], float] = mean,
) -> list[Point]:
    """Group matching records by case and aggregate them into (x, y) points.

    Cases whose runs never produced a successful (status == 'ok') measurement
    for both metrics are skipped, since they have no point to plot.
    """
    cells: dict[str, Cell] = {}
    for record in records:
        if not select(record):
            continue
        name = str(record.get("name") or record.get("file") or "unknown")
        cells.setdefault(name, Cell()).add_record(record)

    points: list[Point] = []
    for name, cell in cells.items():
        xs = cell.metric_values(x_metric)
        ys = cell.metric_values(y_metric)
        if not xs or not ys:
            continue
        points.append(Point(aggregate(xs), aggregate(ys), name))
    return sorted(points, key=lambda point: point.label)


def build_series(
    records: Sequence[dict],
    series: Series,
    aggregate: Callable[[Sequence[float]], float] = mean,
) -> PlotSeries:
    """Resolve a Series description into a renderable PlotSeries."""
    points = build_points(records, series.select, series.x_metric, series.y_metric, aggregate)
    return PlotSeries(series.label, points, series.mark, series.color)


def build_comparison_points(
    records: Sequence[dict],
    x_select: Callable[[dict], bool],
    y_select: Callable[[dict], bool],
    metric: str = "time",
    aggregate: Callable[[Sequence[float]], float] = mean,
) -> list[Point]:
    """Pair two record subsets by case to compare the same metric across them.

    Each point's x is the aggregated ``metric`` of the records matched by
    ``x_select`` for a case, and its y is the aggregated ``metric`` of the
    records matched by ``y_select`` for the same case. Only cases present (with
    successful measurements) in both subsets produce a point. This is the
    natural shape for "tool A vs tool B" scatter plots, optionally drawn against
    a ``y = x`` diagonal.
    """
    x_cells: dict[str, Cell] = {}
    y_cells: dict[str, Cell] = {}
    for record in records:
        name = str(record.get("name") or record.get("file") or "unknown")
        if x_select(record):
            x_cells.setdefault(name, Cell()).add_record(record)
        if y_select(record):
            y_cells.setdefault(name, Cell()).add_record(record)

    points: list[Point] = []
    for name in sorted(x_cells.keys() & y_cells.keys()):
        xs = x_cells[name].metric_values(metric)
        ys = y_cells[name].metric_values(metric)
        if not xs or not ys:
            continue
        points.append(Point(aggregate(xs), aggregate(ys), name))
    return points


def _diagonal_extent(
    series: Sequence[PlotSeries], log_x: bool, log_y: bool
) -> tuple[float, float] | None:
    """Return the (lo, hi) span for a y = x reference line, or None if empty."""
    values: list[float] = []
    for plot_series in series:
        for point in plot_series.points:
            if log_x and point.x <= 0:
                continue
            if log_y and point.y <= 0:
                continue
            values.append(point.x)
            values.append(point.y)
    if not values:
        return None
    return (min(values), max(values))


def render_pgfplots_scatter(
    output_path: Path,
    series: Sequence[PlotSeries],
    x_label: str,
    y_label: str,
    *,
    title: str | None = None,
    log_x: bool = False,
    log_y: bool = False,
    diagonal: bool = False,
    standalone: bool = True,
) -> None:
    """Render scatter series as a pgfplots/TikZ picture written to ``output_path``.

    When ``standalone`` is True the picture is wrapped in a complete,
    compilable ``standalone`` document. When ``diagonal`` is True a dashed
    ``y = x`` reference line spanning every point is added (handy for
    "tool A vs tool B" comparisons). ``log_x`` / ``log_y`` switch the
    respective axis to a logarithmic scale; non-positive coordinates are
    dropped on a logarithmic axis since they cannot be plotted there.
    """
    drawable = [plot_series for plot_series in series if plot_series.points]

    def keep(point: Point) -> bool:
        if log_x and point.x <= 0:
            return False
        if log_y and point.y <= 0:
            return False
        return True

    extent = _diagonal_extent(drawable, log_x, log_y) if diagonal else None

    axis_options = [
        f"xlabel={{{sanitize_str(x_label)}}}",
        f"ylabel={{{sanitize_str(y_label)}}}",
        "grid=both",
        "legend pos=north west",
        "enlargelimits=0.05",
    ]
    if title is not None:
        axis_options.insert(0, f"title={{{sanitize_str(title)}}}")
    if log_x:
        axis_options.append("xmode=log")
    if log_y:
        axis_options.append("ymode=log")

    with open(output_path, "w", encoding="utf-8") as handle:
        if standalone:
            handle.write(r"\documentclass[border=10pt]{standalone}" + "\n")
            handle.write(r"\usepackage{pgfplots}" + "\n")
            handle.write(r"\pgfplotsset{compat=1.18}" + "\n")
            handle.write("\n")
            handle.write(r"\begin{document}" + "\n")
        handle.write(r"\begin{tikzpicture}" + "\n")
        handle.write("\\begin{axis}[\n")
        handle.write("  " + ",\n  ".join(axis_options) + "\n")
        handle.write("]\n")

        for plot_series in drawable:
            handle.write(
                f"\\addplot[only marks, mark={plot_series.mark}, color={plot_series.color}] coordinates {{\n"
            )
            for point in plot_series.points:
                if not keep(point):
                    continue
                handle.write(f"  ({point.x:.6g}, {point.y:.6g})\n")
            handle.write("};\n")
            handle.write(f"\\addlegendentry{{{sanitize_str(plot_series.label)}}}\n")

        if extent is not None:
            lo, hi = extent
            handle.write(
                "\\addplot[no marks, dashed, gray, forget plot] coordinates "
                f"{{({lo:.6g}, {lo:.6g}) ({hi:.6g}, {hi:.6g})}};\n"
            )

        handle.write(r"\end{axis}" + "\n")
        handle.write(r"\end{tikzpicture}" + "\n")
        if standalone:
            handle.write(r"\end{document}" + "\n")


def _output_path_for(inputs: list[Path], output: str | None) -> Path:
    """Compute the output path for the generated scatter plot."""
    if output:
        return Path(output)
    if len(inputs) == 1:
        return inputs[0].with_name(f"{inputs[0].stem}_scatter.tex")
    first_input = inputs[0]
    return first_input.with_name(f"{first_input.stem}_scatter.tex")


def create_plot(
    inputs: list[str],
    x_metric: str = "time",
    y_metric: str = "memory",
    labels: list[str] | None = None,
    output: str | None = None,
    log_x: bool = False,
    log_y: bool = False,
    standalone: bool = True,
    aggregate: str = "mean",
) -> None:
    """Read benchmark result files and generate a pgfplots scatter plot.

    Every input file becomes one series; each benchmark case in that file
    contributes a single aggregated point (``x_metric`` against ``y_metric``,
    time vs memory by default).
    """
    input_paths = [Path(path) for path in inputs]
    if labels is not None and len(labels) != len(input_paths):
        raise ValueError("Provide exactly one --label per input file")

    resolved_labels = labels or [path.stem for path in input_paths]
    aggregator = AGGREGATORS[aggregate]

    series: list[PlotSeries] = []
    for index, (label, path) in enumerate(zip(resolved_labels, input_paths, strict=True)):
        records = load_records(path)
        points = build_points(records, lambda record: True, x_metric, y_metric, aggregator)
        series.append(
            PlotSeries(
                label,
                points,
                DEFAULT_MARKS[index % len(DEFAULT_MARKS)],
                DEFAULT_COLORS[index % len(DEFAULT_COLORS)],
            )
        )

    if not any(plot_series.points for plot_series in series):
        print("No results found.")
        return

    output_path = _output_path_for(input_paths, output)
    render_pgfplots_scatter(
        output_path,
        series,
        METRIC_LABELS.get(x_metric, x_metric),
        METRIC_LABELS.get(y_metric, y_metric),
        log_x=log_x,
        log_y=log_y,
        standalone=standalone,
    )
    print(f"Scatter plot written to {output_path} ({x_metric} vs {y_metric}, aggregate: {aggregate})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate a pgfplots scatter plot from JSON or NDJSON benchmark results."
    )
    parser.add_argument("inputs", nargs="+", help="Path(s) to the JSON or NDJSON results file(s).")
    parser.add_argument(
        "--x-metric", choices=sorted(METRIC_LABELS), default="time",
        help="Metric plotted on the x-axis (default: time).",
    )
    parser.add_argument(
        "--y-metric", choices=sorted(METRIC_LABELS), default="memory",
        help="Metric plotted on the y-axis (default: memory).",
    )
    parser.add_argument(
        "--label", action="append",
        help="Legend label for an input file. Provide this option once per input file.",
    )
    parser.add_argument(
        "--aggregate", choices=sorted(AGGREGATORS), default="mean",
        help="How to combine values across runs of a benchmark case.",
    )
    parser.add_argument("--log-x", action="store_true", help="Use a logarithmic x-axis.")
    parser.add_argument("--log-y", action="store_true", help="Use a logarithmic y-axis.")
    parser.add_argument(
        "--fragment", dest="standalone", action="store_false",
        help="Emit only the tikzpicture fragment instead of a standalone document.",
    )
    parser.add_argument("--output", "-o", help="Path to the generated scatter plot.")
    args = parser.parse_args()

    create_plot(
        args.inputs,
        x_metric=args.x_metric,
        y_metric=args.y_metric,
        labels=args.label,
        output=args.output,
        log_x=args.log_x,
        log_y=args.log_y,
        standalone=args.standalone,
        aggregate=args.aggregate,
    )
