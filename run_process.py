#!/usr/bin/env python

import psutil
import subprocess
import concurrent.futures
import sys
import time


class RunProcess:
    stdout = ""
    stderr = ""
    returncode = -1

    def __init__(
        self,
        tool: str,
        arguments: list[str],
        env: dict[str, str] | None = None,
        max_time: int = sys.maxsize,
        max_memory: int = sys.maxsize,
    ):
        """
        Run the process tool with the given arguments, using at most max_memory MB of resident set memory, and max_time seconds
        """

        try:
            with subprocess.Popen(
                [tool] + arguments,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
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

                        return process

                    except psutil.NoSuchProcess as _:
                        # The tool finished before we could acquire the pid
                        None

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(enforce_limits, proc)
                    stdout, _ = proc.communicate()

                    # Wait for termination
                    future.result()

                    self.stdout = stdout

                self.returncode = proc.returncode
                if proc.returncode != 0:
                    print(self.stderr)
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
