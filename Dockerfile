# syntax=docker/dockerfile:1
# vm-autoscaler image, built on Red Hat UBI 9 + Python 3.12.
#
# Air-gapped builds: point pip at your internal mirror with
#   --build-arg PIP_INDEX_URL=https://pypi-mirror.internal/simple
# Dependencies are pinned *with hashes* in requirements.txt (--require-hashes).

# Base image is pinned by tag AND digest (the digest is what is enforced; the tag is for humans).
# Pinned: ubi9/python-312 1-1795522683 (a.k.a. 9.9-1795522683), manifest list resolved 2026-11-27.
# To refresh: look up the newest tag/digest (skopeo inspect docker://registry.access.redhat.com/ubi9/python-312:latest,
#   or `curl -sI -H 'Accept: application/vnd.docker.distribution.manifest.list.v2+json' \
#    https://registry.access.redhat.com/v2/ubi9/python-312/manifests/latest` -> Docker-Content-Digest),
# then update the default below. Air-gapped: mirror the image and override with
#   --build-arg BASE_IMAGE=<your-registry>/ubi9/python-312@sha256:<digest>
ARG BASE_IMAGE=registry.access.redhat.com/ubi9/python-312:1-1795522683@sha256:5a0471c98fc10017d011c16b7615cd1d7f157d796f89eb4900ca0dc6c87384cf

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
