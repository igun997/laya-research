# syntax=docker/dockerfile:1

# Laya: Convai Innovations' open-source System 1 decision model (Apache 2.0).
# This service runs the model, not the application. `laya-serve` exposes
# POST /v1/systemone, which is the same request/response shape TypeSafe Jev
# serves, so a client written for one works against the other by changing the
# base URL. That compatibility is the whole reason this is a separate container:
# the API talks to a decision model over HTTP instead of importing torch.
#
# CPU-only on purpose. There is no GPU on the target host, so `torch` installs
# the CPU wheel by default and LAYA_DEVICE is pinned to cpu. Measured latency
# for this configuration is recorded in the README.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # torch on PyPI for linux/x86_64 is the CUDA build and drags in several GB of
    # nvidia-* wheels. This host has no GPU, so that is pure waste. The CPU wheel
    # comes from PyTorch's own index instead, which is a two-order-of-magnitude
    # smaller download. Timeouts are raised because the wheel is still large and
    # the default 15s read timeout aborts the build on a slow link.
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=10 \
    # Checkpoints land in a named volume so rebuilds never re-download them.
    HF_HOME=/models \
    HF_HUB_DISABLE_TELEMETRY=1 \
    LAYA_DEVICE=cpu \
    # transformers probes for TensorFlow at import; its abseil runtime can
    # deadlock model construction, and the model card documents this.
    USE_TF=0

WORKDIR /app

# libgomp1 is the OpenMP runtime torch links against and slim does not ship it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

# CPU torch first, from PyTorch's index, so the PyPI resolution below finds
# `torch>=2.0.0` already satisfied and never reaches for the CUDA build.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu "torch>=2.0.0"

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

RUN useradd --create-home --uid 10002 --shell /usr/sbin/nologin laya \
 && mkdir -p /models \
 && chown -R laya:laya /models /app

# Set late so it does not invalidate the expensive apt and pip layers above.
# PyTorch 2.14 otherwise swaps some eager ops for Triton kernels it compiles on
# the first inference, which needs a C compiler slim does not carry: the
# container reports healthy and then fails every request. Documented upstream.
ENV TORCH_DISABLE_NATIVE_JIT=1

USER laya

# Container-internal only. compose never publishes this port: laya-serve binds
# 0.0.0.0 with no authentication unless LAYA_API_KEY is set, so exposing it
# would put an unauthenticated model endpoint on the host network.
EXPOSE 8000

ENTRYPOINT ["laya-serve"]
