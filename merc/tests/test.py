# pylint: disable=missing-docstring
# pylint: disable=line-too-long

import unittest
import sys

from merc import RunProcess, TimeExceededError

class TestRunProcess(unittest.TestCase):

    def test_timeout(self):
        # Check that a process exceeding the time limit raises TimeExceededError
        self.assertRaises(TimeExceededError, RunProcess, sys.executable, ["-c", "import time; time.sleep(2)"], max_time=1)

    def test_printing(self):
        output_lines = []

        def read_stdout(line: str):
            output_lines.append(line.strip())

        # Run a process that prints numbers from 0 to 4
        RunProcess(sys.executable, ["-c", "for i in range(5): print(i)"], read_stdout=read_stdout)

        # Check that the output lines are as expected
        expected_lines = [str(i) for i in range(5)]
        self.assertEqual(output_lines, expected_lines)

    def test_large_output(self):
        output_lines = []

        def read_stdout(line: str):
            output_lines.append(line.strip())

        # Run a process that prints a large number of lines
        RunProcess(sys.executable, ["-c", "for i in range(10000000): print(i, end='')"], read_stdout=read_stdout)        

if __name__ == "__main__":
    unittest.main()
