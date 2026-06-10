import argparse
import json

from pathlib import Path
from collections import defaultdict


def sanitize_str(s: str) -> str:
    """Sanitize a string such that it can be rendered by pdflatex"""
    return s.replace("_", r"\_")


def create_table(path: str) -> None:
    """Read NDJSON benchmark results and generate a LaTeX table."""
    path_obj = Path(path)

    # Group results by name: name -> list of (time_s, memory_mb)
    results: dict[str, list[tuple[float, float]]] = defaultdict(list)
    # Track names that had timeout or error status
    status_override: dict[str, str] = {}

    with open(path_obj, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            name = entry["name"]
            status = entry.get("status", "ok")
            if status == "timeout":
                status_override[name] = "timeout"
            elif status == "error":
                status_override[name] = "error"
            elif status == "ok":
                results[name].append((entry["time_s"], entry["memory_mb"]))

    # Collect all names (including those that only have timeout/error)
    all_names = sorted(set(results.keys()) | set(status_override.keys()))

    if not all_names:
        print("No results found.")
        return

    # Compute averages or mark as timeout/error
    # rows: (name, time_str, memory_str)
    rows: list[tuple[str, str, str]] = []
    for name in all_names:
        if name in status_override:
            if status_override[name] == "timeout":
                rows.append((name, r"\timeout", r"\timeout"))
            else:
                rows.append((name, r"\error", r"\error"))
        else:
            entries = results[name]
            avg_time = sum(t for t, _ in entries) / len(entries)
            avg_memory = sum(m for _, m in entries) / len(entries)
            rows.append((name, f"{avg_time:.2f}", f"{avg_memory:.2f}"))

    # Determine column widths for plain text alignment
    header = ("Name", "Time (s)", "Memory (MB)")
    col_widths = [
        max(len(header[0]), max(len(sanitize_str(r[0])) for r in rows)),
        max(len(header[1]), max(len(r[1]) for r in rows)),
        max(len(header[2]), max(len(r[2]) for r in rows)),
    ]

    # Generate LaTeX table
    output_path = path_obj.with_suffix(".tex")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(r"\newcommand{\timeout}{---}" + "\n")
        f.write(r"\newcommand{\error}{$\times$}" + "\n")
        f.write("\n")
        f.write(r"\begin{tabular}{l r r}" + "\n")
        f.write(
            f"  {header[0]:<{col_widths[0]}} & "
            f"{header[1]:>{col_widths[1]}} & "
            f"{header[2]:>{col_widths[2]}} \\\\\n"
        )
        f.write("  \\hline\n")

        for name, time_str, mem_str in rows:
            f.write(
                f"  {sanitize_str(name):<{col_widths[0]}} & "
                f"{time_str:>{col_widths[1]}} & "
                f"{mem_str:>{col_widths[2]}} \\\\\n"
            )

        f.write(r"\end{tabular}" + "\n")

    print(f"Table written to {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate a LaTeX table from NDJSON benchmark results."
    )
    parser.add_argument("input", help="Path to the NDJSON results file.")
    args = parser.parse_args()

    create_table(args.input)
