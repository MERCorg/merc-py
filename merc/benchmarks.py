import json
from dataclasses import dataclass, field

from .logger import MercLogger
from .run_process import (
    MemoryExceededError,
    RunProcess,
    TimeExceededError,
    ToolNotFoundError,
)


@dataclass
class _Entry:
    name: str
    tool: str
    arguments: list[str]
    extra: dict
    runs: int


class Benchmarks:
    """Collects benchmark configurations and runs them, writing results to NDJSON."""

    def __init__(self, runs: int = 5, logger: MercLogger | None = None):
        self._entries: list[_Entry] = []
        self._default_runs = runs
        self._logger = logger or MercLogger()

    def add(
        self,
        name: str,
        tool: str,
        arguments: list[str],
        extra: dict | None = None,
        runs: int | None = None,
    ) -> None:
        """Register a benchmark.

        Args:
            name:      Human-readable label shown in progress output.
            tool:      Executable path passed to RunProcess.
            arguments: CLI arguments for the tool.
            extra:     Additional fields merged into every result record.
            runs:      Number of repetitions (overrides the class default).
        """
        self._entries.append(
            _Entry(
                name=name,
                tool=tool,
                arguments=arguments,
                extra=extra or {},
                runs=runs if runs is not None else self._default_runs,
            )
        )

    def run(self, output: str = "results.ndjson") -> None:
        """Execute all registered benchmarks and write one NDJSON record per run.

        Raises ToolNotFoundError if a tool binary cannot be found.
        """
        total = sum(e.runs for e in self._entries)
        done = 0

        with open(output, "w", encoding="utf-8") as out:
            for entry in self._entries:
                for run_idx in range(entry.runs):
                    done += 1
                    self._logger.info(
                        "[%d/%d] %s  run %d/%d ...",
                        done,
                        total,
                        entry.name,
                        run_idx + 1,
                        entry.runs,
                    )

                    result = self._run_one(entry.tool, entry.arguments)

                    if result["status"] == "ok":
                        self._logger.info(
                            "  %.2fs  %.1fMB", result["time_s"], result["memory_mb"]
                        )
                    elif result["status"] == "timeout":
                        self._logger.warning("  timeout after %.2fs", result["time_s"])
                    elif result["status"] == "oom":
                        self._logger.warning("  OOM at %.1fMB", result["memory_mb"])
                    else:
                        self._logger.error("  error: %s", result.get("message"))

                    record = {
                        "name": entry.name,
                        "run": run_idx + 1,
                        **entry.extra,
                        **result,
                    }
                    out.write(json.dumps(record) + "\n")
                    out.flush()

        self._logger.info("Results written to %s", output)

    def _run_one(self, tool: str, arguments: list[str]) -> dict:
        try:
            proc = RunProcess(tool, arguments)
            return {"status": "ok", "time_s": proc.user_time, "memory_mb": proc.max_memory}
        except TimeExceededError as e:
            return {"status": "timeout", "time_s": e.value}
        except MemoryExceededError as e:
            return {"status": "oom", "memory_mb": e.value}
        except ToolNotFoundError:
            raise
        except Exception as e:  # pylint: disable=broad-except
            return {"status": "error", "message": str(e)}
