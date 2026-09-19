FROM python:3.14-alpine

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY controller.py /app/controller.py
COPY emergency_restore.py /app/emergency_restore.py

USER 65532:65532
ENTRYPOINT ["python", "/app/controller.py"]
