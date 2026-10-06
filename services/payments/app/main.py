import os
import time
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import create_engine, text

from .otel import configure_otel, shutdown_otel

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://otel:otel@postgres:5432/observability",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
app = FastAPI(title="Payments Service", version="1.0.0")

tracer, meter, logger = configure_otel(app, engine)

payments_authorized = meter.create_counter(
    "payments.authorized",
    description="Number of payments authorized",
    unit="1",
)
payment_duration = meter.create_histogram(
    "payments.authorize.duration",
    description="Payment authorization duration",
    unit="ms",
)

class PaymentRequest(BaseModel):
    order_id: str
    amount: float
    currency: str = "COP"

@app.get("/health")
def health():
    return {"status": "UP", "service": "payments-service"}

@app.post("/payments")
def authorize_payment(request: PaymentRequest):
    start = time.perf_counter()

    # Critical business operation: explicit custom span.
    with tracer.start_as_current_span("payment.authorize") as span:
        span.set_attribute("business.order_id", request.order_id)
        span.set_attribute("business.amount", request.amount)
        span.set_attribute("business.currency", request.currency)

        try:
            # Simulates a small amount of business processing.
            time.sleep(0.020)

            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO payments(order_id, amount, currency, status, created_at)
                        VALUES (:order_id, :amount, :currency, :status, :created)
                        """
                    ),
                    {
                        "order_id": request.order_id,
                        "amount": request.amount,
                        "currency": request.currency,
                        "status": "AUTHORIZED",
                        "created": datetime.now(timezone.utc),
                    },
                )

            payments_authorized.add(
                1,
                {"currency": request.currency, "status": "AUTHORIZED"},
            )
            logger.info(
                "payment authorized",
                extra={
                    "event": "payment.authorized",
                    "order_id": request.order_id,
                },
            )
            return {"order_id": request.order_id, "status": "AUTHORIZED"}

        except Exception as exc:
            span.record_exception(exc)
            span.set_attribute("business.outcome", "error")
            logger.exception(
                "payment failed",
                extra={
                    "event": "payment.failed",
                    "order_id": request.order_id,
                },
            )
            raise HTTPException(status_code=500, detail="payment failed")
        finally:
            payment_duration.record((time.perf_counter() - start) * 1000)

@app.on_event("shutdown")
def shutdown():
    shutdown_otel()
