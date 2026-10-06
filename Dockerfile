# gatehold — stdlib-only, so the image is just Python + the script.
# RUN-free on purpose: COPY-only Dockerfiles build for any arch without qemu.
FROM python:3.12-slim
COPY gatehold.py /usr/local/bin/gatehold
COPY profiles /opt/gatehold/profiles
# No secrets in the image: the queue/audit/profiles live in GATEHOLD_HOME
# (default ~/.gatehold), mounted at runtime. Tools' credentials stay in the
# caller's environment, never baked in.
USER 1000
ENTRYPOINT ["python3", "/usr/local/bin/gatehold"]
