import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

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
    threads: int
    timeout: float | None
    memory_limit: float | None


class Benchmarks:
    """Collects benchmark configurations and runs them, writing results to NDJSON."""

    def __init__(
        self,
        runs: int = 5,
        max_threads: int = 1,
        sequential: bool = False,
        logger: MercLogger | None = None,
        dump_dir: str | None = None,
    ):
        self._entries: list[_Entry] = []
        self._default_runs = runs
        self._max_threads = max_threads
        self._sequential = sequential
        self._logger = logger or MercLogger()
        self._dump_dir = dump_dir

    def add(
        self,
        name: str,
        tool: str,
        arguments: list[str],
        extra: dict | None = None,
        runs: int | None = None,
        threads: int = 1,
        timeout: float | None = None,
        memory_limit: float | None = None,
    ) -> None:
        """Register a benchmark.

        Args:
            name:         Human-readable label shown in progress output.
            tool:         Executable path passed to RunProcess.
            arguments:    CLI arguments for the tool.
            extra:        Additional fields merged into every result record.
            runs:         Number of repetitions (overrides the class default).
            threads:      Thread slots this benchmark occupies (default 1).
            timeout:      Maximum wall-clock seconds before the run is killed.
            memory_limit: Maximum resident memory in MB before the run is killed.
        """
        if threads > self._max_threads:
            raise ValueError(
                f"threads={threads} for '{name}' exceeds max_threads={self._max_threads}"
            )
        self._entries.append(
            _Entry(
                name=name,
                tool=tool,
                arguments=arguments,
                extra=extra or {},
                runs=runs if runs is not None else self._default_runs,
                threads=threads,
                timeout=timeout,
                memory_limit=memory_limit,
            )
        )

    def run(self, output: str = "results.ndjson") -> None:
        """Execute all registered benchmarks and write one NDJSON record per run.

        Raises ToolNotFoundError if a tool binary cannot be found.
        """
        if self._dump_dir:
            os.makedirs(self._dump_dir, exist_ok=True)

        if self._sequential:
            self._run_sequential(output)
        else:
            self._run_parallel(output)

        self._logger.info("Results written to %s", output)

    def _run_sequential(self, output: str) -> None:
        total = sum(e.runs for e in self._entries)
        done = 0

        with open(output, "w", encoding="utf-8") as out:
            for entry in self._entries:
                for run_idx in range(entry.runs):
                    done += 1
                    result = self._run_one(entry)
                    self._log_result(result, done, total, entry, run_idx)
                    record = {"name": entry.name, "run": run_idx + 1, **entry.extra, **result}
                    out.write(json.dumps(record) + "\n")
                    out.flush()

    def _run_parallel(self, output: str) -> None:
        total = sum(e.runs for e in self._entries)
        done = [0]
        done_lock = threading.Lock()
        file_lock = threading.Lock()

        # Weighted semaphore: limits the total thread slots in use across all
        # concurrent jobs so that sum(running threads) <= max_threads.
        available = [self._max_threads]
        slots_cond = threading.Condition()

        def _acquire(n: int) -> None:
            with slots_cond:
                while available[0] < n:
                    slots_cond.wait()
                available[0] -= n

        def _release(n: int) -> None:
            with slots_cond:
                available[0] += n
                slots_cond.notify_all()

        def _run_entry(entry: _Entry, run_idx: int, out) -> None:
            _acquire(entry.threads)
            try:
                result = self._run_one(entry)
            finally:
                _release(entry.threads)

            with done_lock:
                done[0] += 1
                current_done = done[0]

            self._log_result(result, current_done, total, entry, run_idx)

            record = {"name": entry.name, "run": run_idx + 1, **entry.extra, **result}
            with file_lock:
                out.write(json.dumps(record) + "\n")
                out.flush()

        # Submit largest-threads-first so high-slot tasks acquire the semaphore
        # before small tasks get executor OS threads, reducing fragmentation.
        work = sorted(
            ((entry, run_idx) for entry in self._entries for run_idx in range(entry.runs)),
            key=lambda x: x[0].threads,
            reverse=True,
        )

        with open(output, "w", encoding="utf-8") as out:
            with ThreadPoolExecutor(max_workers=self._max_threads) as executor:
                futures = [
                    executor.submit(_run_entry, entry, run_idx, out)
                    for entry, run_idx in work
                ]
                for future in as_completed(futures):
                    future.result()  # re-raise ToolNotFoundError or other exceptions

    def _log_result(self, result: dict, done: int, total: int, entry: _Entry, run_idx: int) -> None:
        if result["status"] == "ok":
            self._logger.info(
                "[%d/%d] %s  run %d/%d  %.2fs  %.1fMB",
                done, total, entry.name, run_idx + 1, entry.runs,
                result["time_s"], result["memory_mb"],
            )
        elif result["status"] == "timeout":
            self._logger.warning(
                "[%d/%d] %s  run %d/%d  timeout after %.2fs",
                done, total, entry.name, run_idx + 1, entry.runs,
                result["time_s"],
            )
        elif result["status"] == "oom":
            self._logger.warning(
                "[%d/%d] %s  run %d/%d  OOM at %.1fMB",
                done, total, entry.name, run_idx + 1, entry.runs,
                result["memory_mb"],
            )
        else:
            self._logger.error(
                "[%d/%d] %s  run %d/%d  error: %s",
                done, total, entry.name, run_idx + 1, entry.runs,
                result.get("message"),
            )

    def _run_one(self, entry: _Entry) -> dict:
        kwargs: dict = {}
        if entry.timeout is not None:
            kwargs["max_time"] = entry.timeout
        if entry.memory_limit is not None:
            kwargs["max_memory"] = entry.memory_limit

        stdout_f = None
        stderr_f = None
        try:
            if self._dump_dir:
                # buffering=1 gives line-buffered writes so each line hits the file immediately.
                # Each run opens its own handle in append mode; O_APPEND makes concurrent
                # writes from parallel runs of the same benchmark atomic at the OS level.
                stdout_f = open(
                    os.path.join(self._dump_dir, f"{entry.name}.stdout"),
                    "a", encoding="utf-8", buffering=1,
                )
                stderr_f = open(
                    os.path.join(self._dump_dir, f"{entry.name}.stderr"),
                    "a", encoding="utf-8", buffering=1,
                )

            try:
                proc = RunProcess(
                    entry.tool,
                    entry.arguments,
                    read_stdout=lambda line: stdout_f.write(line + "\n") if stdout_f else None,
                    read_stderr=lambda line: stderr_f.write(line + "\n") if stderr_f else None,
                    **kwargs,
                )
                return {"status": "ok", "time_s": proc.user_time, "memory_mb": proc.max_memory}
            except TimeExceededError as e:
                return {"status": "timeout", "time_s": e.value}
            except MemoryExceededError as e:
                return {"status": "oom", "memory_mb": e.value}
            except ToolNotFoundError:
                raise
            except Exception as e:  # pylint: disable=broad-except
                return {"status": "error", "message": str(e)}
        finally:
            if stdout_f:
                stdout_f.close()
            if stderr_f:
                stderr_f.close()
