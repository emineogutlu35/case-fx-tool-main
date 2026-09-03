# Notes

## Decisions

For weekends and ECB holidays, I accept an earlier rate only when Frankfurter
returns the date that rate belongs to. The response keeps the requested date in
`asked_date` and exposes the actual date in `rate_date`. A later-dated, missing
or malformed rate is rejected instead of guessed.

I reject future and pre-series dates, unsupported or identical currencies, and
non-positive or overly precise amounts. I use `Decimal` for calculation and
cache only validated historical rates using the date and currency pair.

## With another day

I would add bounded cache eviction, structured logs and metrics. I would also
test concurrent identical requests and decide whether they should share one
in-flight upstream call.

## Test warnings

All 16 tests pass successfully. The test run reports two deprecation warnings
from the installed FastAPI, Starlette, HTTPX, and AnyIO dependency versions.
These warnings originate from `TestClient` internals rather than the application
or test logic, and they do not affect the test results. With more time, I would
align or upgrade the dependency versions to remove these warnings.

## Challenging part

The most challenging part was making the HTTP client easy to replace in
offline tests. I solved this by moving client creation into a separate function
and mocking it with `MockTransport`.

## AI tools

I used Codex to inspect the brief, discuss edge cases, draft implementation and
tests, and check the final files. I reviewed the code, chose the public API
behaviour, and ran the offline test suite myself.

## One thing the AI got wrong

The first HTTP-client design created `httpx.AsyncClient` directly inside the
request function. That made clean offline injection harder. I noticed this
while preparing the tests and changed it to a small `create_http_client`
function that tests can replace with `MockTransport`.
