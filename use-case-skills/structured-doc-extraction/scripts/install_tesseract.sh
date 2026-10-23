#!/bin/bash
# Cluster init script for the localize task of the structured-doc-extraction use-case skill (module 06).
# render.py copies it to <name>-extract/src/, and the OCR job cluster in databricks.yml runs it at startup.
# apt-get needs egress to the Ubuntu package mirrors; in a workspace without it, mirror the package instead.
set -euo pipefail
if ! command -v tesseract >/dev/null 2>&1; then
  apt-get update -y
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends tesseract-ocr
fi
tesseract --version
