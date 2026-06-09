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