#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# CPU wheels avoid downloading the CUDA dependency stack on a local CPU host.
uv pip install torch==2.14.1+cpu torchvision==0.29.1+cpu --index-url https://download.pytorch.org/whl/cpu
uv pip install -e '.[docling,paddle]'
# Install last: overlapping OpenCV distributions otherwise require desktop libGL.
uv pip install --reinstall opencv-python-headless==5.0.0.93
.venv/bin/python -c 'import cv2, paddle, docling; print("Local OCR imports are ready")'
