import os
import time
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import create_engine, text

from .otel import configure_otel, shutdown_otel

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://otel:otel@postgres:5432/observability",
)
PAYMENTS_URL = os.getenv("PAYMENTS_URL", "http://payments-service:8001")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
app = FastAPI(title="Orders Service", version="1.0.0")

tracer, meter, logger = configure_otel(app, engine)

orders_created = meter.create_counter(
    "orders.created",
    description="Number of orders created",
    unit="1",
)
order_duration = meter.create_histogram(
    "orders.create.duration",
    description="Order creation duration",
    unit="ms",
)

class OrderRequest(BaseModel):
    customer_id: str
    amount: float
    currency: str = "COP"

@app.get("/health")
def health():
    return {"status": "UP", "service": "orders-service"}

@app.post("/orders")
def create_order(request: OrderRequest):
    start = time.perf_counter()
    order_id = str(uuid.uuid4())

    # Critical business operation: explicit custom span.
    with tracer.start_as_current_span("order.create") as span:
        span.set_attribute("business.order_id", order_id)
        span.set_attribute("business.customer_id", request.customer_id)
        span.set_attribute("business.amount", request.amount)
        span.set_attribute("business.currency", request.currency)

        try:
            with engine.begin() as conn:
                conn.execute(
                    text(
                        """
                        INSERT INTO orders(order_id, customer_id, amount, currency, status, created_at)
                        VALUES (:id, :customer, :amount, :currency, :status, :created)
                        """
                    ),
                    {
                        "id": order_id,
                        "customer": request.customer_id,
                        "amount": request.amount,
                        "currency": request.currency,
                        "status": "PENDING",
                        "created": datetime.now(timezone.utc),
                    },
                )

            logger.info(
                "order persisted",
                extra={"event": "order.persisted", "order_id": order_id},
            )

            # HTTPX instrumentation propagates W3C trace context to payments-service.
            with httpx.Client(timeout=3.0) as client:
                response = client.post(
                    f"{PAYMENTS_URL}/payments",
                    json={
                        "order_id": order_id,
                        "amount": request.amount,
                        "currency": request.currency,
                    },
                )
                response.raise_for_status()

            with engine.begin() as conn:
                conn.execute(
                    text("UPDATE orders SET status = :status WHERE order_id = :id"),
                    {"status": "PAID", "id": order_id},
                )

            orders_created.add(
                1,
                {"currency": request.currency, "status": "PAID"},
            )
            logger.info(
                "order completed",
                extra={"event": "order.completed", "order_id": order_id},
            )
            return {"order_id": order_id, "status": "PAID"}

        except Exception as exc:
            span.record_exception(exc)
            span.set_attribute("business.outcome", "error")
            logger.exception(
                "order failed",
                extra={"event": "order.failed", "order_id": order_id},
            )
            raise HTTPException(status_code=500, detail="order processing failed")
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            order_duration.record(elapsed_ms)

@app.on_event("shutdown")
def shutdown():
    shutdown_otel()
