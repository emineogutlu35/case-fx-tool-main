# Review of tool.py

## 1. Failures become successful zero-value conversions

The broad `except Exception` block returns HTTP 200 with `rate: 0.0` and
`result: 0.0` for timeouts, bad JSON, upstream errors and programming errors.
A customer-facing model cannot distinguish this from a real conversion and may
tell a paying customer that their money is worth zero.

I would verify this by pointing the upstream to a closed port, then by returning
HTTP 500 and an HTML body. Each request currently returns a successful-looking
zero conversion instead of a non-2xx error.

## 2. The cache can attach one day's rate to another date

The cache key contains only the currency pair. It does not contain the asked
date. On a cache hit, the function returns the cached rate but labels it with
the new requested date. This can silently produce a wrong invoice, quote or
payment amount while claiming that the rate belongs to the customer's date.

I would verify this with a fake upstream that returns different rates for two
dates. I would request the same pair for both dates and show that the second
request reuses the first rate and reports the second date.

## 3. The public contract and upstream configuration are not honoured

The endpoint accepts `from_` and `on`, while the documented tool sends `from`
and `date`. FastAPI therefore ignores the caller's source currency and date and
uses the defaults. The real upstream URL is also hardcoded, so reviewers cannot
redirect it with `FX_UPSTREAM_BASE`. There is no explicit timeout or status
check, and the rate is rounded to two decimals before conversion. Together
these issues can return a wrong number or keep a customer waiting.

I would verify this by calling the documented URL with USD, GBP and a known
historical date, then inspecting the outgoing request. It uses EUR and latest.
I would also set `FX_UPSTREAM_BASE` to a fake server and confirm it is not
contacted, delay the hardcoded upstream, and compare a precise rate with the
prematurely rounded result.

## The one I would fix before shipping tonight

I would fix finding 1 first. A visible non-2xx failure lets the calling model
stop safely, while the current HTTP 200 response presents fabricated zeroes as
real financial data. Findings 2 and 3 are also release blockers.

## Things that look suspicious but are fine

An in-memory cache is reasonable for this small, single-process service; the
defect is its missing date and incorrect metadata, not the lack of a database.
Reusing one global `AsyncClient` is also good for connection pooling, although
it should be closed during shutdown. The extra `/health` endpoint is unnecessary
for the exercise but does not itself harm customers.
