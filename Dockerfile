# syntax=docker/dockerfile:1
# vm-autoscaler image, built on Red Hat UBI 9 + Python 3.12.
#
# Air-gapped builds: point pip at your internal mirror with
#   --build-arg PIP_INDEX_URL=https://pypi-mirror.internal/simple
# Dependencies are pinned *with hashes* in requirements.txt (--require-hashes).

# For repeatable (and air-gapped) builds, pin the base image by digest from your registry:
#   --build-arg BASE_IMAGE=registry.access.redhat.com/ubi9/python-312@sha256:<digest>
# (find it with: skopeo inspect docker://registry.access.redhat.com/ubi9/python-312:latest)
ARG BASE_IMAGE=registry.access.redhat.com/ubi9/python-312:latest

# ---------- build stage: create a venv with pinned dependencies ----------
FROM ${BASE_IMAGE} AS build
ARG PIP_INDEX_URL
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
USER 0
COPY requirements.txt /tmp/requirements.txt
RUN python3.12 -m venv /opt/venv \
 && /opt/venv/bin/pip install --require-hashes --no-deps -r /tmp/requirements.txt

# ---------- runtime stage ----------
FROM ${BASE_IMAGE}
LABEL name="vm-autoscaler" \
      summary="GitOps autoscaler for VictoriaMetrics vmstorage on OpenShift" \
      io.k8s.description="Receives Grafana alerts and scales vmstorage via the GitOps backend and Argo CD"

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    CONFIG_PATH=/etc/vm-autoscaler/config.yaml

USER 0
COPY --from=build /opt/venv /opt/venv
COPY app /opt/vm-autoscaler/app
# Optional, only for ARGOCD_SYNC_METHOD=cli: ship the argocd binary from your mirror.
# COPY argocd /usr/local/bin/argocd

# OpenShift runs the container with a random UID in group 0: give group 0 the same
# permissions as the owner. Nothing needs to be writable except /tmp (an emptyDir).
RUN chgrp -R 0 /opt/vm-autoscaler /opt/venv \
 && chmod -R g=u /opt/vm-autoscaler /opt/venv

WORKDIR /opt/vm-autoscaler
USER 1001
EXPOSE 8080

# uvicorn is PID 1 (exec form) and gets SIGTERM directly. It stops accepting new
# connections, waits up to 80s for in-flight webhooks, then runs the app shutdown.
# Keep 80 + the preStop sleep below terminationGracePeriodSeconds (deployment.yaml).
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", \
     "--no-access-log", "--proxy-headers", "--timeout-graceful-shutdown", "80"]
