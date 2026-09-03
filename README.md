# FX Conversion Tool

A small FastAPI service that converts an amount using ECB reference rates from
Frankfurter. It has one tool endpoint and never returns an invented rate.

## Setup and run

Python 3.11 or newer is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./run.sh
```

On Windows, activate the environment with `.venv\Scripts\Activate.ps1`, then
run the scripts from Git Bash.

The service reads these environment variables:

- `FX_UPSTREAM_BASE`: upstream base URL; defaults to `https://api.frankfurter.dev`.
- `PORT`: listening port; defaults to `8080`.

Example request:

```text
GET /tools/convert?amount=250&from=EUR&to=TRY&date=2026-08-28
```

Example successful response:

```json
{
  "amount": 250.0,
  "from": "EUR",
  "to": "TRY",
  "rate": 47.1234,
  "result": 11780.85,
  "rate_date": "2026-08-28",
  "asked_date": "2026-08-28",
  "source": "ECB via frankfurter.dev"
}
```

## Tests

```bash
./test.sh
```

Tests replace the HTTP client with `httpx.MockTransport`; they do not require
Frankfurter or any network connection. They also pass with a closed upstream:

```bash
FX_UPSTREAM_BASE=http://127.0.0.1:1 ./test.sh
```

## Behaviour and safety choices

- On an ECB weekend or holiday, Frankfurter may return the previous published
  rate. The service accepts it only when its date is not later than the asked
  date. `asked_date` keeps the customer's date and `rate_date` shows the actual
  publication date.
- Future dates and dates before the ECB series start (`1999-01-04`) are
  rejected before an upstream request is made.
- Currency codes are normalized to uppercase and must contain exactly three
  ASCII letters. Unknown currencies and conversions to the same currency are
  rejected.
- Amounts must be positive and have at most two decimal places. Calculations
  use `Decimal`, and the result is rounded to two places with `ROUND_HALF_UP`.
- Timeouts, connection failures, upstream errors, non-JSON bodies, invalid
  dates, missing fields and invalid rates return non-2xx errors. No zero or
  guessed conversion is returned.
- Validated historical rates are cached in memory by asked date and currency
  pair. Repeating a question does not call the upstream again. The cache is
  local to one running process.

## Error codes

- `invalid_request` (422): a required parameter is missing or malformed.
- `invalid_amount` (400): the amount is zero, negative or not finite.
- `amount_precision` (400): the amount has more than two decimal places.
- `future_date` (400): the requested date is in the future.
- `date_before_series` (400): the date is before `1999-01-04`.
- `same_currency` (400): source and target currencies are the same.
- `unsupported_currency` (400): a currency code is invalid or unsupported.
- `rate_not_available` (404): no rate is available for the requested date.
- `upstream_unavailable` (503): the provider timed out or could not be reached.
- `upstream_error` (502): the provider returned an HTTP error.
- `invalid_upstream_response` (502): the provider returned non-JSON or invalid
  rate data.

All error responses have this shape:

```json
{
  "error": "short_machine_code",
  "message": "A sentence a person can read."
}
```
