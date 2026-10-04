# syntax=docker/dockerfile:1.7
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

ARG TORCH_VERSION=2.14.1+cpu
ARG TORCHVISION_VERSION=0.29.1+cpu
ARG OPENCV_HEADLESS_VERSION=5.0.0.93
ARG NUMPY_VERSION=2.3.5

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/studio \
    XDG_CACHE_HOME=/home/studio/.cache \
    HF_HOME=/home/studio/.cache/huggingface \
    HF_HUB_DISABLE_TELEMETRY=1 \
    DO_NOT_TRACK=1 \
    INV_STUDIO_DATA=/tmp/inv-studio-data \
    PORT=8080

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-dejavu-core \
        libglib2.0-0 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 studio \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/studio --shell /usr/sbin/nologin studio

WORKDIR /opt/inv-studio

# Copy only reproducible application inputs. Runtime state, credentials and local
# caches are excluded by both ignore files and are never copied into this image.
COPY pyproject.toml LICENSE ./
# Install dependencies against a minimal package, so code-only changes can reuse
# this expensive CPU OCR layer. Runtime imports resolve from /opt/inv-studio.
RUN mkdir -p app && touch app/__init__.py

RUN python -m pip install --no-cache-dir \
        pip==25.0.1 setuptools==78.1.0 wheel==0.45.1 \
    && python -m pip install --no-cache-dir --no-build-isolation '.[cloud]' \
    && python -m pip install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cpu \
        "torch==${TORCH_VERSION}" "torchvision==${TORCHVISION_VERSION}" \
    && python -m pip install --no-cache-dir --no-build-isolation '.[docling,paddle]' \
    && python -m pip install --no-cache-dir --force-reinstall \
        "numpy==${NUMPY_VERSION}" \
        "opencv-python-headless==${OPENCV_HEADLESS_VERSION}" \
    && python -m pip check \
    && python -c 'import cv2, docling, paddle, psycopg; print("Cloud OCR imports are ready")'

ARG CLAUDE_VERSION=2.1.285
ARG CLAUDE_SHA256=33dad1ec615a2e08cc78b494f05c110e49916de2c79d78ec8799ebf46b233d29
RUN python - <<'PY'
import hashlib, os, pathlib, urllib.request
url='https://downloads.claude.ai/claude-code-releases/'+os.environ['CLAUDE_VERSION']+'/linux-x64/claude'
target=pathlib.Path('/usr/local/bin/claude')
urllib.request.urlretrieve(url,target)
assert hashlib.sha256(target.read_bytes()).hexdigest()==os.environ['CLAUDE_SHA256'], 'CLI checksum mismatch'
target.chmod(0o755)
PY
ENV DISABLE_UPDATES=1
RUN claude --version

COPY app ./app
COPY samples ./samples
COPY README.md ./
COPY scripts/warm_ocr.py ./scripts/warm_ocr.py

RUN mkdir -p \
        /home/studio/.cache/huggingface \
        /home/studio/.paddlex \
        /tmp/inv-studio-data/uploads \
        /tmp/inv-studio-data/exports \
        /tmp/inv-studio-data/templates \
        /tmp/inv-studio-data/work \
    && chown -R 1000:1000 /home/studio /tmp/inv-studio-data

# RapidOCR 3.9.2 keeps its recognition dictionaries in this data directory,
# independently of Global.model_root_dir. Only this cache is writable, not code.
RUN python -c 'import importlib.util,os; from pathlib import Path; p=Path(importlib.util.find_spec("rapidocr").origin).parent/"models"; p.mkdir(exist_ok=True); os.chown(p,1000,1000)'

USER 1000:1000

# Download public model artifacts during the build and prove each reader can
# parse the bundled synthetic invoice. A missing/corrupt model fails the image.
RUN python scripts/warm_ocr.py

EXPOSE 8080
STOPSIGNAL SIGTERM

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
