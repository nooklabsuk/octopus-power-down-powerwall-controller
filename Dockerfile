FROM python:3.14-alpine

ARG VERSION=dev
ENV POWERDOWN_CONTROLLER_VERSION=${VERSION}

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY controller.py /app/controller.py
COPY emergency_restore.py /app/emergency_restore.py

# Pre-create /state owned by the non-root runtime user so that Docker-managed
# named volumes inherit correct write permissions automatically on first
# mount, without requiring users to manually chown the volume.
RUN mkdir -p /state && chown 65532:65532 /state
VOLUME ["/state"]

USER 65532:65532
ENTRYPOINT ["python", "/app/controller.py"]
