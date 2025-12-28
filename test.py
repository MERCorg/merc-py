import unittest
import sys

from run_process import RunProcess, TimeExceededError

class TestRunProcess(unittest.TestCase):

    def test_timeout(self):
        # Check that a process exceeding the time limit raises TimeExceededError
        self.assertRaises(TimeExceededError, RunProcess, sys.executable, ["-c", "import time; time.sleep(2)"], max_time=1)

if __name__ == "__main_":
    unittest.main()
