#!/usr/bin/env python

# pylint: disable=pointless-statement
# pylint: disable=missing-docstring
# pylint: disable=line-too-long

import subprocess
import concurrent.futures
import sys
import time

from typing import Callable

import psutil


class RunProcess:
    stdout = ""
    stderr = ""
    returncode = -1

    def __init__(
        self,
        tool: str,
        arguments: list[str],
        read_stdout: Callable[[str], None] | None = None,
        env: dict[str, str] | None = None,
        max_time: int = sys.maxsize,
        max_memory: int = sys.maxsize,
    ):
        """
        Run the process tool with the given arguments, using at most max_memory MB of resident set memory, and max_time seconds
        """

        try:
            # Use a separate timer to measure user time
            before = time.perf_counter()
            with subprocess.Popen(
                [tool] + arguments,
                # Merge stderr and stdout into one, so we don't have to handle both streams in separate threads.
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                # Pass the environment
                env=env,
                # 1 means line buffered (only usable if text=True or universal_newlines=True)
                text=True,
                bufsize=1,
            ) as proc:
                self._user_time = 0
                self._max_memory_used = 0

                # Start a thread to limit the process memory and time usage.
                def enforce_limits(proc):
                    try:
                        process = psutil.Process(proc.pid)
                        while proc.returncode is None:
                            m = process.memory_info()

                            # Update max memory used
                            self._max_memory_used = max(
                                self._max_memory_used, m.rss / 1024 / 1024
                            )

                            if self._max_memory_used > max_memory:
                                kill_all(process)
                                raise MemoryExceededError(
                                    tool, self._max_memory_used, max_memory
                                )

                            if self._user_time > max_time:
                                kill_all(process)
                                raise TimeExceededError(tool, self._user_time, max_time)
                            self._user_time += 0.1
                            time.sleep(0.1)

                    except psutil.NoSuchProcess as _:
                        # The tool finished before we could acquire the pid
                        None  # type: ignore

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(enforce_limits, proc)

                    # Process the output while the process is running
                    if proc.stdout:
                        for line in proc.stdout:
                            if read_stdout:
                                read_stdout(line.rstrip("\n"))
                    
                    # EOF was reached, wait for process to terminate (if it hasn't already)
                    proc.wait()

                    # Wait for the limit enforcement thread to finish
                    future.result()

                # Use the realtime to measure user time more accurately
                self._user_time = time.perf_counter() - before

                if proc.returncode != 0:
                    raise ToolRuntimeError(
                        f"Tool {tool} {arguments} ended with return code {proc.returncode}"
                    )

        except FileNotFoundError as e:
            raise ToolNotFoundError(tool) from e

    @property
    def user_time(self):
        return self._user_time

    @property
    def max_memory(self):
        return self._max_memory_used


class TimeExceededError(Exception):
    def __init__(self, name: str, value: float, max_time: float):
        self.name = name
        self.value = value
        self.max_time = max_time

    def __str__(self):
        return f"Process {self.name} exceeded time after {self.value:.2f}s of max {self.max_time:.2f}s"


class MemoryExceededError(Exception):
    def __init__(self, name: str, value: float, max_memory: float):
        self.name = name
        self.value = value
        self.max_memory = max_memory

    def __str__(self):
        return f"Process {self.name} exceeded memory with {self.value:.2f}MB of max {self.max_memory:.2f}MB"


class ToolNotFoundError(Exception):
    def __init__(self, name: str):
        self.name = name

    def __str__(self):
        return f"Process {self.name} does not exist!"


class ToolRuntimeError(Exception):
    def __init__(self, value: str):
        self.value = value

    def __str__(self):
        return repr(self.value)


def kill_all(process):
    """Kill a process tree (including grandchildren) with signal
    "sig" and return a (gone, still_alive) tuple.
    """
    children = process.children(recursive=True)
    children.append(process)

    for p in children:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(children)
    assert not alive
