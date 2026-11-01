# Databricks notebook source
# MAGIC %md
# MAGIC # <title>
# MAGIC
# MAGIC # smith: what this job task does, in one or two sentences
# MAGIC
# MAGIC The job passes its settings as the widgets at the top; to run it by hand, fill them in. To keep a change you
# MAGIC make here, export this notebook into the bundle's `src/` folder before the next deploy, which otherwise replaces it.

# COMMAND ----------

# smith: a `# MAGIC %pip install -q "<package spec>"` line for each package the serverless environment lacks, or delete this cell

# COMMAND ----------

"""<title>: a job task of <skill-name> (module <module-id>).

It's a Databricks notebook: it runs as a notebook task of the skill's job, whose parameters arrive as widgets, or
locally through Databricks Connect, with the same settings as flags.
"""

import argparse
import os
from pathlib import Path

# smith: <module-id>: port <sources> here; every setting is a flag in build_parser(), passed from the state file

# COMMAND ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=(__doc__ or "").partition("\n")[0])  # a notebook has no __doc__
    # smith: one flag per setting, such as p.add_argument("--catalog", required=True)
    return p


def notebook_argv(parser: argparse.ArgumentParser) -> list[str]:
    """The notebook's widgets as command-line flags. A job's notebook task passes each setting as a widget named
    like its flag (--volume-path is volume_path); a required one left empty is reported as missing."""
    argv = []
    for action in parser._actions:  # noqa: SLF001 - argparse has no public list of options
        if not action.option_strings or action.dest == "help":
            continue
        dbutils.widgets.text(action.dest, "" if action.default is None else str(action.default))  # noqa: F821
        value = dbutils.widgets.get(action.dest)  # noqa: F821 - dbutils exists in Databricks notebooks
        if value or action.default is not None:
            argv += [action.option_strings[0], value]
    return argv


def script_dir() -> Path:
    """This file's folder: next to the script, or a notebook's own workspace folder, its working directory."""
    return Path(__file__).resolve().parent if "__file__" in globals() else Path.cwd()


def get_spark():
    if "DATABRICKS_RUNTIME_VERSION" in os.environ:
        from pyspark.sql import SparkSession

        return SparkSession.builder.getOrCreate()
    from databricks.connect import DatabricksSession

    return DatabricksSession.builder.serverless().getOrCreate()

# COMMAND ----------

def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    spark = get_spark()
    # smith: the task's logic, with args and spark

# COMMAND ----------

# As a job's notebook task, the settings arrive as widgets; run as a script, as command-line flags.
if __name__ == "__main__":
    main(notebook_argv(build_parser()) if "dbutils" in globals() else None)
