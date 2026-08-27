# Overview

A set of Python utilities used for artifacts. The core feature is `Benchmarks`, which runs external tools repeatedly, measures their wall-clock time and peak memory, and writes one NDJSON record per run. `RunProcess` handles execution and resource enforcement; `MercLogger` captures log output in memory while also writing to stderr.

## Example

```python
from merc.benchmarks import Benchmarks

bench = Benchmarks(runs=3, max_threads=4)
bench.add("my-tool baseline", tool="/usr/bin/my-tool", arguments=["--input", "data.bin"], extra={"version": "1.0"})
bench.add("my-tool optimized", tool="/usr/bin/my-tool", arguments=["--input", "data.bin", "--opt"], extra={"version": "2.0"})
bench.run("results.ndjson")
```

Each run appends a line like `{"name": "my-tool baseline", "run": 1, "version": "1.0", "status": "ok", "time_s": 1.23, "memory_mb": 45.6}` to `results.ndjson`.

## Usage

This module can be installed via pip:

```python
pip install merc-py
```

## Testing

The Python scripts can be tested using the built-in `unittest` framework. To run
the tests, use the following command:

```bash
python -m unittest discover -s merc/tests
```


### `merc-py/merc/create_table.py`

Generates a LaTeX table from JSON or NDJSON benchmark output.

- With one input file, it keeps the old summary behavior.
- With multiple input files, it merges rows by benchmark name plus the selected merge keys.
- Each input contributes a `Time (s)` and `Memory (MB)` column for side-by-side comparison.

```bash
python3 merc-py/merc/create_table.py results.ndjson

python3 merc-py/merc/create_table.py results-a.ndjson results-b.ndjson \
	--merge-key threads \
	--merge-key caching \
	--label baseline \
	--label optimized \
	-o comparison.tex
```

### `merc-py/merc/create_plot.py`

The scatter-plot counterpart of `create_table.py`. Generates a dependency-free
pgfplots/TikZ scatter plot from JSON or NDJSON benchmark output (compiles with a
stock TeX distribution; no Python plotting libraries required).

- Every input file becomes one series.
- Each benchmark case contributes a single aggregated point (`Time (s)` on the
  x-axis and `Memory (MB)` on the y-axis by default).
- Use `--x-metric` / `--y-metric`, `--log-x` / `--log-y`, and `--fragment` to
  customize the axes and output.

```bash
python3 merc-py/merc/create_plot.py results.ndjson --log-x --log-y

python3 merc-py/merc/create_plot.py results-a.ndjson results-b.ndjson \
	--label baseline \
	--label optimized \
	-o comparison_scatter.tex
```