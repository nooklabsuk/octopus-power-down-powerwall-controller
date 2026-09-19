FROM python:3.14-alpine

WORKDIR /app
COPY controller.py /app/controller.py
COPY emergency_restore.py /app/emergency_restore.py

USER 65532:65532
ENTRYPOINT ["python", "/app/controller.py"]
