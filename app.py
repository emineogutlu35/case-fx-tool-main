from __future__ import annotations

import os
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, TypedDict

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

# Create the FastAPI application.
app = FastAPI(title="FX Conversion Tool", version="1.0.0")

# Read the API address from the environment.
UPSTREAM_BASE = os.getenv(
    "FX_UPSTREAM_BASE",
    "https://api.frankfurter.dev",
).rstrip("/")
UPSTREAM_TIMEOUT = 3.0
ECB_SERIES_START = date(1999, 1, 4)


# Describe the data stored in the cache.
class CachedRate(TypedDict):
    rate: Decimal
    rate_date: str


# Store validated rates in memory.
_rate_cache: dict[tuple[str, str, str], CachedRate] = {}


# Create the HTTP client in one place so tests can replace it.
def create_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT)


# Create the standard error response.
def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": code, "message": message},
    )


# Return a simple error for invalid query parameters.
@app.exception_handler(RequestValidationError)
async def validation_error_response(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    return error_response(
        422,
        "invalid_request",
        "The request is missing a required parameter or contains an invalid value.",
    )


# Check the data returned by the upstream API.
def parse_upstream_payload(
    payload: Any,
    to_currency: str,
    asked_date: date,
) -> tuple[Decimal, str] | JSONResponse:
    # The response must be a JSON object.
    if not isinstance(payload, dict):
        return error_response(
            502,
            "invalid_upstream_response",
            "The exchange-rate provider returned an invalid response.",
        )

    rate_date_value = payload.get("date")
    rates = payload.get("rates")

    # The response must contain a date and a rates object.
    if not isinstance(rate_date_value, str) or not isinstance(rates, dict):
        return error_response(
            502,
            "invalid_upstream_response",
            "The exchange-rate provider returned an invalid response.",
        )

    # Change the date text into a date object.
    try:
        parsed_rate_date = date.fromisoformat(rate_date_value)
    except ValueError:
        return error_response(
            502,
            "invalid_upstream_response",
            "The exchange-rate provider returned an invalid rate date.",
        )

    # Never use a rate from a later date.
    if parsed_rate_date > asked_date:
        return error_response(
            502,
            "invalid_upstream_response",
            "The exchange-rate provider returned a rate from a later date.",
        )

    # The target currency must exist in the response.
    if to_currency not in rates:
        return error_response(
            400,
            "unsupported_currency",
            f"'{to_currency}' is not a supported currency code.",
        )

    # Use Decimal to keep the rate accurate.
    try:
        rate = Decimal(str(rates[to_currency]))
    except (InvalidOperation, TypeError, ValueError):
        return error_response(
            502,
            "invalid_upstream_response",
            "The exchange-rate provider returned an invalid rate.",
        )

    # The rate must be a positive, normal number.
    if not rate.is_finite() or rate <= 0:
        return error_response(
            502,
            "invalid_upstream_response",
            "The exchange-rate provider returned an invalid rate.",
        )

    return rate, parsed_rate_date.isoformat()


# Get a rate from the cache or the upstream API.
async def fetch_rate(
    asked_date: date,
    from_currency: str,
    to_currency: str,
) -> tuple[Decimal, str] | JSONResponse:
    # Include the date and both currencies in the cache key.
    cache_key = (asked_date.isoformat(), from_currency, to_currency)
    cached = _rate_cache.get(cache_key)

    # Return the saved rate when it exists.
    if cached is not None:
        return cached["rate"], cached["rate_date"]

    # Build the URL from the configured base address.
    url = f"{UPSTREAM_BASE}/v1/{asked_date.isoformat()}"

    # Call the upstream API with a timeout.
    try:
        async with create_http_client() as client:
            response = await client.get(
                url,
                params={"base": from_currency, "symbols": to_currency},
            )
    except httpx.TimeoutException:
        return error_response(
            503,
            "upstream_unavailable",
            "The exchange-rate provider did not respond in time.",
        )
    except httpx.RequestError:
        return error_response(
            503,
            "upstream_unavailable",
            "The exchange-rate provider is currently unavailable.",
        )

    # Check the HTTP status.
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError:
        if response.status_code in {400, 422}:
            return error_response(
                400,
                "unsupported_currency",
                "One or both currency codes are not supported.",
            )
        if response.status_code == 404:
            return error_response(
                404,
                "rate_not_available",
                "No exchange rate is available for the requested date.",
            )
        return error_response(
            502,
            "upstream_error",
            "The exchange-rate provider returned an error.",
        )

    # Read the response as JSON.
    try:
        payload = response.json()
    except ValueError:
        return error_response(
            502,
            "invalid_upstream_response",
            "The exchange-rate provider returned non-JSON content.",
        )

    # Validate the JSON data.
    parsed = parse_upstream_payload(payload, to_currency, asked_date)
    if isinstance(parsed, JSONResponse):
        return parsed

    rate, rate_date = parsed

    # Save only validated data in the cache.
    _rate_cache[cache_key] = {"rate": rate, "rate_date": rate_date}
    return rate, rate_date


# Create the currency conversion endpoint.
@app.get("/tools/convert", response_model=None)
async def convert(
    amount: Decimal,
    from_currency: str = Query(alias="from"),
    to_currency: str = Query(alias="to"),
    asked_date: date = Query(alias="date"),
) -> dict[str, object] | JSONResponse:
    # Normalize currency codes.
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()

    # The amount must be greater than zero.
    if not amount.is_finite() or amount <= 0:
        return error_response(400, "invalid_amount", "Amount must be greater than zero.")

    # The amount can have at most two decimal places.
    if amount.as_tuple().exponent < -2:
        return error_response(
            400,
            "amount_precision",
            "Amount may have at most two decimal places.",
        )

    # The date cannot be in the future.
    if asked_date > date.today():
        return error_response(
            400,
            "future_date",
            "The requested date cannot be in the future.",
        )

    # The date must be inside the ECB series.
    if asked_date < ECB_SERIES_START:
        return error_response(
            400,
            "date_before_series",
            "The requested date is before the ECB exchange-rate series began.",
        )

    # The currencies must be different.
    if from_currency == to_currency:
        return error_response(
            400,
            "same_currency",
            "Source and target currencies must be different.",
        )

    # Currency codes must contain three ASCII letters.
    for currency in (from_currency, to_currency):
        if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
            return error_response(
                400,
                "unsupported_currency",
                f"'{currency}' is not a supported currency code.",
            )

    # Get the exchange rate.
    fetched = await fetch_rate(asked_date, from_currency, to_currency)
    if isinstance(fetched, JSONResponse):
        return fetched

    rate, rate_date = fetched
    # Calculate and round the result.
    result = (amount * rate).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )

    # Return the successful response.
    return {
        "amount": float(amount),
        "from": from_currency,
        "to": to_currency,
        "rate": float(rate),
        "result": float(result),
        "rate_date": rate_date,
        "asked_date": asked_date.isoformat(),
        "source": "ECB via frankfurter.dev",
    }
