FROM python:3.12-slim
WORKDIR /app
COPY module-c/security/requirements-probe.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY services/orders/app/security.py /app/services/orders/app/security.py
COPY module-c/security/probe_app.py /app/probe_app.py
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEPLOYMENT_ENVIRONMENT=sandbox \
    SECURITY_AUTH_ENABLED=true \
    OTEL_SERVICE_NAME=module-c-auth-probe
USER 65534:65534
EXPOSE 8080
CMD ["uvicorn", "probe_app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
