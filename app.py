from __future__ import annotations

import os
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


# Create the API app.
app = FastAPI(title="FX Conversion Tool", version="1.0.0")


# Set the first valid date and wait time.
ECB_SERIES_START = date(1999, 1, 4)
UPSTREAM_TIMEOUT_SECONDS = 3.0


# Save old rates here.
_rate_cache: dict[tuple[str, str, str], tuple[Decimal, str]] = {}


# Create a custom error.
class ServiceError(Exception):
    """An error that can safely be returned to the API caller."""

    def __init__(
        self,
        status_code: int,
        error: str,
        message: str,
    ) -> None:
        self.status_code = status_code
        self.error = error
        self.message = message
        super().__init__(message)


# Send custom errors as JSON.
@app.exception_handler(ServiceError)
async def handle_service_error(
    request: Request,
    exc: ServiceError,
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": exc.error,
            "message": exc.message,
        },
    )


# Send an error for wrong input.
@app.exception_handler(RequestValidationError)
async def handle_validation_error(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": "invalid_request",
            "message": (
                "The request is missing a required parameter or contains "
                "an invalid value."
            ),
        },
    )


# Get the rate API address.
def get_upstream_base() -> str:
    """Read the upstream URL from the environment."""

    return os.getenv(
        "FX_UPSTREAM_BASE",
        "https://api.frankfurter.dev",
    ).rstrip("/")


# Check the currency code.
def validate_currency(currency: str) -> str:
    """Normalize and perform basic validation on a currency code."""

    normalized = currency.strip().upper()

    if len(normalized) != 3 or not normalized.isalpha():
        raise ServiceError(
            status_code=400,
            error="unsupported_currency",
            message=f"'{currency}' is not a supported currency code.",
        )

    return normalized


# Check the money amount.
def validate_amount(amount: Decimal) -> None:
    """Require a positive monetary amount with at most two decimal places."""

    # The amount must be more than zero.
    if not amount.is_finite() or amount <= 0:
        raise ServiceError(
            status_code=400,
            error="invalid_amount",
            message="Amount must be greater than zero.",
        )

    decimal_places = max(0, -amount.as_tuple().exponent)

    # Use no more than two decimal places.
    if decimal_places > 2:
        raise ServiceError(
            status_code=400,
            error="amount_precision",
            message="Amount may have at most two decimal places.",
        )


# Check the data from the rate API.
def parse_upstream_payload(
    payload: Any,
    target_currency: str,
    asked_date: date,
) -> tuple[Decimal, str]:
    """Validate the upstream response and extract its rate and real date."""

    # The data must be an object.
    if not isinstance(payload, dict):
        raise ServiceError(
            status_code=502,
            error="invalid_upstream_response",
            message="The exchange-rate provider returned an invalid response.",
        )

    rate_date_value = payload.get("date")
    rates = payload.get("rates")

    # Check the date and rates.
    if not isinstance(rate_date_value, str) or not isinstance(rates, dict):
        raise ServiceError(
            status_code=502,
            error="invalid_upstream_response",
            message="The exchange-rate provider returned an invalid response.",
        )

    # Change the date text to a date object.
    try:
        rate_date = date.fromisoformat(rate_date_value)
    except ValueError as exc:
        raise ServiceError(
            status_code=502,
            error="invalid_upstream_response",
            message="The exchange-rate provider returned an invalid rate date.",
        ) from exc

    # The rate date cannot be later.
    if rate_date > asked_date:
        raise ServiceError(
            status_code=502,
            error="invalid_upstream_response",
            message="The exchange-rate provider returned a rate from a later date.",
        )

    # Get the wanted rate.
    raw_rate = rates.get(target_currency)

    if raw_rate is None:
        raise ServiceError(
            status_code=400,
            error="unsupported_currency",
            message=f"'{target_currency}' is not a supported currency code.",
        )

    # Change the rate to Decimal.
    try:
        rate = Decimal(str(raw_rate))
    except (InvalidOperation, ValueError) as exc:
        raise ServiceError(
            status_code=502,
            error="invalid_upstream_response",
            message="The exchange-rate provider returned an invalid rate.",
        ) from exc

    # The rate must be more than zero.
    if not rate.is_finite() or rate <= 0:
        raise ServiceError(
            status_code=502,
            error="invalid_upstream_response",
            message="The exchange-rate provider returned an invalid rate.",
        )

    return rate, rate_date.isoformat()


# Get the rate from memory or the rate API.
async def fetch_rate(
    base_currency: str,
    target_currency: str,
    asked_date: date,
) -> tuple[Decimal, str]:
    """Fetch and validate one historical exchange rate."""

    # Create a key for this rate.
    cache_key = (
        asked_date.isoformat(),
        base_currency,
        target_currency,
    )

    # Use the saved rate if it exists.
    cached_rate = _rate_cache.get(cache_key)

    if cached_rate is not None:
        return cached_rate

    # Create the request address.
    url = f"{get_upstream_base()}/v1/{asked_date.isoformat()}"

    # Ask the rate API for the rate.
    try:
        async with httpx.AsyncClient(
            timeout=UPSTREAM_TIMEOUT_SECONDS,
        ) as client:
            response = await client.get(
                url,
                params={
                    "base": base_currency,
                    "symbols": target_currency,
                },
            )
    except httpx.TimeoutException as exc:
        raise ServiceError(
            status_code=503,
            error="upstream_unavailable",
            message="The exchange-rate provider did not respond in time.",
        ) from exc
    except httpx.RequestError as exc:
        raise ServiceError(
            status_code=503,
            error="upstream_unavailable",
            message="The exchange-rate provider is currently unavailable.",
        ) from exc

    # Check errors from the rate API.
    if response.status_code >= 500:
        raise ServiceError(
            status_code=502,
            error="upstream_error",
            message="The exchange-rate provider returned a server error.",
        )

    if response.status_code in {400, 422}:
        raise ServiceError(
            status_code=400,
            error="unsupported_currency",
            message="One or both currency codes are not supported.",
        )

    if response.status_code == 404:
        raise ServiceError(
            status_code=404,
            error="rate_not_available",
            message="No exchange rate is available for the requested date.",
        )

    if not 200 <= response.status_code < 300:
        raise ServiceError(
            status_code=502,
            error="upstream_error",
            message="The exchange-rate provider returned an unexpected error.",
        )

    # Read the answer as JSON.
    try:
        payload = response.json()
    except ValueError as exc:
        raise ServiceError(
            status_code=502,
            error="invalid_upstream_response",
            message="The exchange-rate provider returned non-JSON content.",
        ) from exc

    # Get the rate and date.
    rate, rate_date = parse_upstream_payload(
        payload=payload,
        target_currency=target_currency,
        asked_date=asked_date,
    )

    # Save the rate for later.
    _rate_cache[cache_key] = (rate, rate_date)

    return rate, rate_date


# Create the money change endpoint.
@app.get("/tools/convert")
async def convert(
    amount: Decimal,
    from_currency: str = Query(alias="from"),
    to_currency: str = Query(alias="to"),
    asked_date: date = Query(alias="date"),
) -> dict[str, object]:
    """Convert an amount using an ECB exchange rate."""

    # Check the user input.
    validate_amount(amount)

    base_currency = validate_currency(from_currency)
    target_currency = validate_currency(to_currency)

    # The two currencies cannot be the same.
    if base_currency == target_currency:
        raise ServiceError(
            status_code=400,
            error="same_currency",
            message="Source and target currencies must be different.",
        )

    today = date.today()

    # The date cannot be in the future.
    if asked_date > today:
        raise ServiceError(
            status_code=400,
            error="future_date",
            message="The requested date cannot be in the future.",
        )

    # The date cannot be too old.
    if asked_date < ECB_SERIES_START:
        raise ServiceError(
            status_code=400,
            error="date_before_series",
            message="The requested date is before the ECB rate series began.",
        )

    # Get the exchange rate.
    rate, rate_date = await fetch_rate(
        base_currency=base_currency,
        target_currency=target_currency,
        asked_date=asked_date,
    )

    # Calculate and round the result.
    result = (amount * rate).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )

    # Send the result as JSON.
    return {
        "amount": float(amount),
        "from": base_currency,
        "to": target_currency,
        "rate": float(rate),
        "result": float(result),
        "rate_date": rate_date,
        "asked_date": asked_date.isoformat(),
        "source": "ECB via frankfurter.dev",
    }