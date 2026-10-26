"""Entry point for each forge_ucs_drift job task (and local development runs).

    python run.py <detect|analyze|propose|publish|all> --run-id <id> [--dry-run true] [--local-dir DIR --no-llm]
"""

import os
import sys

# spark_python_task runs this file as a script; put the drift package next to it on the path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__ if "__file__" in globals() else sys.argv[0])))

from drift.config import parse_args  # noqa: E402
from drift.steps import run  # noqa: E402

if __name__ == "__main__":
    run(*parse_args())
