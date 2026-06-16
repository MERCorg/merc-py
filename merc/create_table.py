import argparse
import json

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean, median
from typing import Callable, Sequence


def sanitize_str(s: str) -> str:
    """Sanitize a string such that it can be rendered by pdflatex"""
    return s.replace("_", r"\_")


STATUS_PRIORITY = {
    "ok": 0,
    "timeout": 1,
    "oom": 2,
    "error": 3,
}

STATUS_LATEX = {
    "timeout": r"\timeout",
    "oom": r"\oom",
    "error": r"\error",
}


def pretty_header(key: str) -> str:
    """Convert a record key into a readable table header."""
    if key == "cache_key":
        return "Cache key"
    return key.replace("_", " ").title()


def display_label(path: Path) -> str:
    """Use the input file stem as the default table label."""
    return path.stem


def normalize_key_value(entry: dict, key: str) -> str | int | float:
    """Resolve a merge key value from a benchmark record."""
    if key == "cache_key":
        return entry.get("cache_key") or entry.get("caching") or "unknown"

    value = entry.get(key, "")
    if value is None:
        return ""
    return value


def sort_key_value(value: str | int | float) -> tuple[int, float | str]:
    """Sort numerically when possible and lexicographically otherwise."""
    if isinstance(value, (int, float)):
        return (0, value)

    text = str(value)
    try:
        return (0, float(text))
    except ValueError:
        return (1, text)


def worse_status(current: str, new: str) -> str:
    """Keep the most severe status seen for a cell."""
    current_priority = STATUS_PRIORITY.get(current, STATUS_PRIORITY["error"])
    new_priority = STATUS_PRIORITY.get(new, STATUS_PRIORITY["error"])
    return new if new_priority > current_priority else current


def load_records(path: Path) -> list[dict]:
    """Load either a JSON array/object or an NDJSON file."""
    with open(path, encoding="utf-8") as handle:
        raw_text = handle.read().strip()

    if not raw_text:
        return []

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        records: list[dict] = []
        for line_number, line in enumerate(raw_text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            record = json.loads(stripped)
            if not isinstance(record, dict):
                raise ValueError(f"{path}:{line_number} does not contain a JSON object")
            records.append(record)
        return records

    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        for index, record in enumerate(parsed, start=1):
            if not isinstance(record, dict):
                raise ValueError(f"{path}: item {index} is not a JSON object")
        return parsed

    raise ValueError(f"{path} does not contain a JSON object, array, or NDJSON stream")


def default_merge_keys(records_by_input: list[tuple[str, list[dict]]]) -> list[str]:
    """Choose defaults that preserve single-file behavior and help multi-file comparisons."""
    if len(records_by_input) == 1:
        return ["cache_key"]

    available_keys = []
    for _, records in records_by_input:
        keys = {key for record in records for key in record.keys()}
        available_keys.append(keys)

    if available_keys and all("threads" in keys for keys in available_keys):
        return ["threads"]
    return ["cache_key"]


def output_path_for(inputs: list[Path], output: str | None) -> Path:
    """Compute the output path for the generated LaTeX table."""
    if output:
        return Path(output)
    if len(inputs) == 1:
        return inputs[0].with_suffix(".tex")
    first_input = inputs[0]
    return first_input.with_name(f"{first_input.stem}_comparison.tex")


def render_cell(stats: dict[str, object] | None) -> tuple[str, str]:
    """Convert aggregated cell statistics into LaTeX-ready strings."""
    if stats is None:
        return (r"\na", r"\na")

    status = str(stats["status"])
    if status != "ok":
        token = STATUS_LATEX.get(status, r"\error")
        return (token, token)

    values = stats["values"]
    if not values:
        return (r"\na", r"\na")

    time_values = [time_s for time_s, _ in values]
    memory_values = [memory_mb for _, memory_mb in values]
    avg_time = sum(time_values) / len(time_values)
    avg_memory = sum(memory_values) / len(memory_values)
    return (f"{avg_time:.2f}", f"{avg_memory:.2f}")


# ---------------------------------------------------------------------------
# Generic table interface
#
# The primitives below are reusable building blocks for assembling LaTeX
# tables from benchmark records. They are meant to be imported and
# "instantiated" by benchmark-specific scripts (for example
# create_table_exploration.py), which describe their own rows and columns.
# ---------------------------------------------------------------------------

AGGREGATORS: dict[str, Callable[[Sequence[float]], float]] = {
    "mean": lambda values: float(mean(values)),
    "sum": lambda values: float(sum(values)),
    "median": lambda values: float(median(values)),
}


@dataclass
class Cell:
    """Collects the successful (status == 'ok') measurements for one table cell."""

    times: list[float] = field(default_factory=list)
    memories: list[float] = field(default_factory=list)
    record_count: int = 0
    status: str | None = None

    def add_record(self, record: dict) -> None:
        self.record_count += 1
        status = str(record.get("status", "ok"))
        self.status = status if self.status is None else worse_status(self.status, status)
        if status != "ok":
            return
        if "time_s" in record and "memory_mb" in record:
            self.times.append(float(record["time_s"]))
            self.memories.append(float(record["memory_mb"]))

    def metric_values(self, metric: str) -> list[float]:
        if metric == "time":
            return self.times
        if metric == "memory":
            return self.memories
        raise ValueError(f"Unknown metric: {metric!r}")


@dataclass
class Column:
    """A single data column: nested header labels, a record filter, and a metric."""

    path: tuple[str, ...]
    select: Callable[[dict], bool]
    metric: str
    fmt: str = "{:.2f}"


@dataclass
class Row:
    """A table row: leading label cells and a record filter."""

    labels: tuple[str, ...]
    select: Callable[[dict], bool]


def render_metric(cell: Cell, column: Column, aggregate: Callable[[Sequence[float]], float]) -> str:
    """Aggregate the measurements collected for a cell into a LaTeX string.

    Cells with successful measurements render the aggregated number. Cells that
    only contain failing runs render the status token (for example ``\\timeout``
    or ``\\oom``). Cells with no matching records at all render ``\\na``.
    """
    values = cell.metric_values(column.metric)
    if values:
        return column.fmt.format(aggregate(values))
    if cell.record_count == 0:
        return r"\na"
    return STATUS_LATEX.get(cell.status or "error", r"\error")


def header_rows(column_paths: Sequence[tuple[str, ...]]) -> list[list[tuple[str, int]]]:
    """Merge consecutive columns sharing a header prefix into (label, span) groups."""
    if not column_paths:
        return []
    depth = len(column_paths[0])
    rows: list[list[tuple[str, int]]] = []
    for level in range(depth):
        groups: list[tuple[str, int]] = []
        index = 0
        while index < len(column_paths):
            end = index
            while (
                end < len(column_paths)
                and column_paths[end][: level + 1] == column_paths[index][: level + 1]
            ):
                end += 1
            groups.append((column_paths[index][level], end - index))
            index = end
        rows.append(groups)
    return rows


def build_table(
    records: Sequence[dict],
    rows: Sequence[Row],
    columns: Sequence[Column],
    aggregate: Callable[[Sequence[float]], float] = mean,
) -> list[list[str]]:
    """Compute the rendered string for every cell of the table."""
    table_rows: list[list[str]] = []
    for row in rows:
        row_records = [record for record in records if row.select(record)]
        rendered = list(row.labels)
        for column in columns:
            cell = Cell()
            for record in row_records:
                if column.select(record):
                    cell.add_record(record)
            rendered.append(render_metric(cell, column, aggregate))
        table_rows.append(rendered)
    return table_rows


def render_latex_table(
    output_path: Path,
    row_headers: Sequence[str],
    columns: Sequence[Column],
    table_rows: Sequence[Sequence[str]],
    compare_key: Callable[[Column], object] | None = None,
    standalone: bool = False,
) -> None:
    """Render a LaTeX tabular with (possibly) multi-level column headers.

    When ``compare_key`` is provided, columns that share the same (non-``None``)
    key form a comparison group; within each row, the smallest numeric value of
    every group is rendered in boldface.

    When ``standalone`` is True, the tabular is wrapped in a complete,
    compilable ``standalone`` document whose single page is cropped to the size
    of the table (so the whole table fits on one giant page).
    """
    column_paths = [column.path for column in columns]
    levels = header_rows(column_paths)
    leading = len(row_headers)
    alignment = " ".join(["l"] * leading + ["r"] * len(columns))
    total_columns = leading + len(columns)

    # Group data-column indices (offset by the leading columns) for comparison.
    comparison_groups: dict[object, list[int]] = defaultdict(list)
    if compare_key is not None:
        for offset, column in enumerate(columns):
            key = compare_key(column)
            if key is not None:
                comparison_groups[key].append(leading + offset)

    def parse_number(text: str) -> float | None:
        try:
            return float(text)
        except ValueError:
            return None

    def bold_flags(row: Sequence[str]) -> list[bool]:
        flags = [False] * total_columns
        for indices in comparison_groups.values():
            best_value = None
            best_indices: list[int] = []
            for index in indices:
                value = parse_number(row[index])
                if value is None:
                    continue
                if best_value is None or value < best_value:
                    best_value = value
                    best_indices = [index]
                elif value == best_value:
                    best_indices.append(index)
            for index in best_indices:
                flags[index] = True
        return flags

    # Render every data cell to its final LaTeX string (sanitized + emphasized)
    # up front so column widths account for the \textbf{...} markup and the
    # source columns stay aligned.
    rendered_rows: list[list[str]] = []
    for row in table_rows:
        flags = bold_flags(row)
        cells = []
        for index, value in enumerate(row):
            text = sanitize_str(value)
            if flags[index]:
                text = rf"\textbf{{{text}}}"
            cells.append(text)
        rendered_rows.append(cells)

    leaf_header_cells = [sanitize_str(header) for header in row_headers]
    leaf_header_cells += [sanitize_str(column.path[-1]) for column in columns]
    col_widths = [len(cell) for cell in leaf_header_cells]
    for cells in rendered_rows:
        for index in range(total_columns):
            col_widths[index] = max(col_widths[index], len(cells[index]))

    def pad(text: str, index: int) -> str:
        width = col_widths[index]
        # Leading columns are left-aligned (l), data columns right-aligned (r).
        return f"{text:<{width}}" if index < leading else f"{text:>{width}}"

    with open(output_path, "w", encoding="utf-8") as handle:
        if standalone:
            handle.write(r"\documentclass[border=10pt]{standalone}" + "\n")
            handle.write(r"\usepackage{array}" + "\n")
            handle.write("\n")
        handle.write(r"\newcommand{\timeout}{---}" + "\n")
        handle.write(r"\newcommand{\oom}{OOM}" + "\n")
        handle.write(r"\newcommand{\error}{$\times$}" + "\n")
        handle.write(r"\newcommand{\na}{n/a}" + "\n")
        handle.write("\n")
        if standalone:
            handle.write(r"\begin{document}" + "\n")
        handle.write(rf"\begin{{tabular}}{{{alignment}}}" + "\n")
        handle.write("  \\hline\n")

        last_level = len(levels) - 1
        for level, groups in enumerate(levels):
            if level == last_level:
                cells = [pad(sanitize_str(header), index) for index, header in enumerate(row_headers)]
            else:
                cells = [pad("", index) for index in range(leading)]
            column_index = leading
            for label, span in groups:
                text = sanitize_str(label)
                # Width spanned: the column widths plus the " & " separators
                # between the spanned columns.
                span_width = sum(col_widths[column_index:column_index + span]) + 3 * (span - 1)
                if span == 1:
                    cells.append(pad(text, column_index))
                else:
                    content = rf"\multicolumn{{{span}}}{{c}}{{{text}}}"
                    cells.append(f"{content:<{span_width}}")
                column_index += span
            handle.write("  " + " & ".join(cells) + r" \\" + "\n")
        handle.write("  \\hline\n")

        for cells in rendered_rows:
            formatted = [pad(value, index) for index, value in enumerate(cells)]
            handle.write("  " + " & ".join(formatted) + r" \\" + "\n")
        handle.write("  \\hline\n")
        handle.write(r"\end{tabular}" + "\n")
        if standalone:
            handle.write(r"\end{document}" + "\n")


def create_table(inputs: list[str], merge_keys: list[str] | None = None, labels: list[str] | None = None,
                 output: str | None = None) -> None:
    """Read benchmark result files and generate a LaTeX comparison table."""
    input_paths = [Path(path) for path in inputs]
    if labels is not None and len(labels) != len(input_paths):
        raise ValueError("Provide exactly one --label per input file")

    resolved_labels = labels or [display_label(path) for path in input_paths]
    records_by_input = [
        (label, load_records(path))
        for label, path in zip(resolved_labels, input_paths, strict=True)
    ]

    resolved_merge_keys = merge_keys or default_merge_keys(records_by_input)

    # rows[name + merge keys][input label] -> {status, values}
    rows_by_key: dict[tuple[object, ...], dict[str, dict[str, object]]] = defaultdict(dict)

    for label, records in records_by_input:
        for entry in records:
            name = entry.get("name") or entry.get("file") or "unknown"
            row_key = tuple([name] + [normalize_key_value(entry, key) for key in resolved_merge_keys])

            cell = rows_by_key[row_key].setdefault(label, {"status": "ok", "values": []})
            status = str(entry.get("status", "ok"))
            cell["status"] = worse_status(str(cell["status"]), status)

            if status == "ok" and "time_s" in entry and "memory_mb" in entry:
                cell["values"].append((float(entry["time_s"]), float(entry["memory_mb"])))

    if not rows_by_key:
        print("No results found.")
        return

    sorted_row_keys = sorted(
        rows_by_key,
        key=lambda row_key: tuple(sort_key_value(value) for value in row_key),
    )

    headers = ["Name", *[pretty_header(key) for key in resolved_merge_keys]]
    for label in resolved_labels:
        headers.extend([f"{label} Time (s)", f"{label} Memory (MB)"])

    row_values: list[list[str]] = []
    for row_key in sorted_row_keys:
        values = [str(row_key[0]), *[str(value) for value in row_key[1:]]]
        for label in resolved_labels:
            time_str, memory_str = render_cell(rows_by_key[row_key].get(label))
            values.extend([time_str, memory_str])
        row_values.append(values)

    col_widths = []
    for col_index, header in enumerate(headers):
        width = len(sanitize_str(header))
        for row in row_values:
            width = max(width, len(sanitize_str(row[col_index])))
        col_widths.append(width)

    alignment = " ".join(["l"] * (1 + len(resolved_merge_keys)) + ["r"] * (2 * len(resolved_labels)))
    output_path = output_path_for(input_paths, output)
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write(r"\newcommand{\timeout}{---}" + "\n")
        handle.write(r"\newcommand{\oom}{OOM}" + "\n")
        handle.write(r"\newcommand{\error}{$\times$}" + "\n")
        handle.write(r"\newcommand{\na}{n/a}" + "\n")
        handle.write("\n")
        handle.write(rf"\begin{{tabular}}{{{alignment}}}" + "\n")
        handle.write(
            "  " + " & ".join(
                f"{sanitize_str(header):<{col_widths[index]}}"
                for index, header in enumerate(headers)
            ) + r" \\" + "\n"
        )
        handle.write("  \\hline\n")

        for row in row_values:
            formatted = []
            for index, value in enumerate(row):
                sanitized = sanitize_str(value)
                if index < 1 + len(resolved_merge_keys):
                    formatted.append(f"{sanitized:<{col_widths[index]}}")
                else:
                    formatted.append(f"{sanitized:>{col_widths[index]}}")
            handle.write("  " + " & ".join(formatted) + r" \\" + "\n")

        handle.write(r"\end{tabular}" + "\n")

    merge_key_desc = ", ".join(resolved_merge_keys) if resolved_merge_keys else "name"
    print(f"Table written to {output_path} (merged on: name, {merge_key_desc})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate a LaTeX comparison table from JSON or NDJSON benchmark results."
    )
    parser.add_argument("inputs", nargs="+", help="Path(s) to the JSON or NDJSON results file(s).")
    parser.add_argument(
        "--merge-key",
        dest="merge_keys",
        action="append",
        help=(
            "Additional key to merge rows on. The benchmark name is always included. "
            "Repeat this option to merge on multiple keys, for example: --merge-key threads --merge-key caching"
        ),
    )
    parser.add_argument(
        "--label",
        action="append",
        help="Column label for an input file. Provide this option once per input file.",
    )
    parser.add_argument("--output", "-o", help="Path to the generated LaTeX table.")
    args = parser.parse_args()

    create_table(args.inputs, merge_keys=args.merge_keys, labels=args.label, output=args.output)
