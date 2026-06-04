#!/usr/bin/env bash
set -euo pipefail
MODEL_OUT=models/model.ckpt
python src/app/main.py
python scripts/export.py --output ${MODEL_OUT}
make package
