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


def _load_existing_counts(output: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not os.path.exists(output):
        return counts
    with open(output, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if "name" in record:
                    counts[record["name"]] = counts.get(record["name"], 0) + 1
            except json.JSONDecodeError:
                pass
    return counts


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

        If *output* already exists, benchmarks whose name appears in it are
        skipped and new results are appended rather than overwriting the file.

        Raises ToolNotFoundError if a tool binary cannot be found.
        """
        if self._dump_dir:
            os.makedirs(self._dump_dir, exist_ok=True)

        existing = _load_existing_counts(output)
        for name, count in existing.items():
            self._logger.info("Found %d existing run(s) for '%s' in %s", count, name, output)

        if self._sequential:
            self._run_sequential(output, existing)
        else:
            self._run_parallel(output, existing)

        self._logger.info("Results written to %s", output)

    def _pending_work(
        self, existing: dict[str, int]
    ) -> tuple[list[tuple[_Entry, int, int]], int]:
        """Return (work_list, total_runs) for entries that still need runs."""
        work: list[tuple[_Entry, int, int]] = []
        for e in self._entries:
            already_done = existing.get(e.name, 0)
            remaining = e.runs - already_done
            if remaining > 0:
                work.append((e, already_done, remaining))
        total = sum(remaining for _, _, remaining in work)
        return work, total

    @staticmethod
    def _make_record(entry: _Entry, run_idx: int, result: dict) -> str:
        """Build an NDJSON line for one benchmark run."""
        record = {"name": entry.name, "run": run_idx + 1, **entry.extra, **result}
        return json.dumps(record) + "\n"

    def _run_sequential(self, output: str, existing: dict[str, int]) -> None:
        work, total = self._pending_work(existing)
        done = 0

        with open(output, "a", encoding="utf-8") as out:
            for entry, already_done, remaining in work:
                for i in range(remaining):
                    run_idx = already_done + i
                    done += 1
                    result = self._run_one(entry, run_idx)
                    self._log_result(result, done, total, entry, run_idx)
                    out.write(self._make_record(entry, run_idx, result))
                    out.flush()

    def _run_parallel(self, output: str, existing: dict[str, int]) -> None:
        work_entries, total = self._pending_work(existing)
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
                result = self._run_one(entry, run_idx)
            finally:
                _release(entry.threads)

            with done_lock:
                done[0] += 1
                current_done = done[0]

            self._log_result(result, current_done, total, entry, run_idx)

            with file_lock:
                out.write(self._make_record(entry, run_idx, result))
                out.flush()

        # Submit largest-threads-first so high-slot tasks acquire the semaphore
        # before small tasks get executor OS threads, reducing fragmentation.
        work = sorted(
            ((entry, already_done + i) for entry, already_done, remaining in work_entries for i in range(remaining)),
            key=lambda x: x[0].threads,
            reverse=True,
        )

        with open(output, "a", encoding="utf-8") as out:
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

    def _run_one(self, entry: _Entry, run_idx: int) -> dict:
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
                stdout_f = open(
                    os.path.join(self._dump_dir, f"{entry.name}_{run_idx}.stdout"),
                    "w", encoding="utf-8", buffering=1,
                )
                stderr_f = open(
                    os.path.join(self._dump_dir, f"{entry.name}_{run_idx}.stderr"),
                    "w", encoding="utf-8", buffering=1,
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
