import httpx
import pytest
from fastapi.testclient import TestClient

import app as fx_app


# Clear the cache before every test.
@pytest.fixture(autouse=True)
def clear_rate_cache() -> None:
    fx_app._rate_cache.clear()


# Create a test client for the FastAPI app.
@pytest.fixture
def client() -> TestClient:
    with TestClient(fx_app.app) as test_client:
        yield test_client


# Replace the real HTTP client with a fake one.
def use_fake_upstream(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    transport = httpx.MockTransport(handler)

    def create_fake_client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=transport)

    monkeypatch.setattr(fx_app, "create_http_client", create_fake_client)


def convert_params(**changes: str) -> dict[str, str]:
    params = {
        "amount": "250",
        "from": "EUR",
        "to": "TRY",
        "date": "2024-01-05",
    }
    params.update(changes)
    return params


# Test a normal working-day conversion.
def test_successful_conversion(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/2024-01-05"
        assert request.url.params["base"] == "EUR"
        assert request.url.params["symbols"] == "TRY"
        return httpx.Response(
            200,
            json={"date": "2024-01-05", "base": "EUR", "rates": {"TRY": 32.5}},
        )

    use_fake_upstream(monkeypatch, handler)
    response = client.get("/tools/convert", params=convert_params())

    assert response.status_code == 200
    assert response.json() == {
        "amount": 250.0,
        "from": "EUR",
        "to": "TRY",
        "rate": 32.5,
        "result": 8125.0,
        "rate_date": "2024-01-05",
        "asked_date": "2024-01-05",
        "source": "ECB via frankfurter.dev",
    }


# Test that a weekend can use the previous published rate.
def test_weekend_shows_real_rate_date(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"date": "2024-01-05", "base": "EUR", "rates": {"TRY": 32.5}},
        )

    use_fake_upstream(monkeypatch, handler)
    response = client.get(
        "/tools/convert",
        params=convert_params(date="2024-01-06"),
    )

    assert response.status_code == 200
    assert response.json()["asked_date"] == "2024-01-06"
    assert response.json()["rate_date"] == "2024-01-05"


# Test that the same request uses the cache.
def test_same_request_calls_upstream_once(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200,
            json={"date": "2024-01-05", "base": "EUR", "rates": {"TRY": 32.5}},
        )

    use_fake_upstream(monkeypatch, handler)
    first = client.get("/tools/convert", params=convert_params())
    second = client.get("/tools/convert", params=convert_params())

    assert first.status_code == 200
    assert second.status_code == 200
    assert call_count == 1


# Test dates that the service must reject.
@pytest.mark.parametrize(
    ("asked_date", "error_code"),
    [
        ("2999-01-01", "future_date"),
        ("1999-01-03", "date_before_series"),
    ],
)
def test_invalid_dates(
    client: TestClient,
    asked_date: str,
    error_code: str,
) -> None:
    response = client.get(
        "/tools/convert",
        params=convert_params(date=asked_date),
    )

    assert response.status_code == 400
    assert response.json()["error"] == error_code


# Test that source and target currencies must be different.
def test_same_currency(client: TestClient) -> None:
    response = client.get(
        "/tools/convert",
        params=convert_params(to="EUR"),
    )

    assert response.status_code == 400
    assert response.json()["error"] == "same_currency"


# Test an unsupported currency returned by the upstream API.
def test_unsupported_currency(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "Invalid currency"})

    use_fake_upstream(monkeypatch, handler)
    response = client.get(
        "/tools/convert",
        params=convert_params(to="ZZZ"),
    )

    assert response.status_code == 400
    assert response.json()["error"] == "unsupported_currency"


# Test missing, zero, negative, and overly precise amounts.
@pytest.mark.parametrize(
    ("amount", "status_code", "error_code"),
    [
        (None, 422, "invalid_request"),
        ("0", 400, "invalid_amount"),
        ("-10", 400, "invalid_amount"),
        ("1.1234567890", 400, "amount_precision"),
    ],
)
def test_invalid_amounts(
    client: TestClient,
    amount: str | None,
    status_code: int,
    error_code: str,
) -> None:
    params = convert_params()
    if amount is None:
        params.pop("amount")
    else:
        params["amount"] = amount

    response = client.get("/tools/convert", params=params)

    assert response.status_code == status_code
    assert response.json()["error"] == error_code


# Test an upstream timeout.
def test_upstream_timeout(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Timed out", request=request)

    use_fake_upstream(monkeypatch, handler)
    response = client.get("/tools/convert", params=convert_params())

    assert response.status_code == 503
    assert response.json()["error"] == "upstream_unavailable"


# Test an upstream server error.
def test_upstream_server_error(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "Server error"})

    use_fake_upstream(monkeypatch, handler)
    response = client.get("/tools/convert", params=convert_params())

    assert response.status_code == 502
    assert response.json()["error"] == "upstream_error"


# Test a response that is not JSON.
def test_non_json_upstream_response(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    use_fake_upstream(monkeypatch, handler)
    response = client.get("/tools/convert", params=convert_params())

    assert response.status_code == 502
    assert response.json()["error"] == "invalid_upstream_response"


# Test JSON responses with missing fields.
@pytest.mark.parametrize(
    "payload",
    [
        {"rates": {"TRY": 32.5}},
        {"date": "2024-01-05"},
    ],
)
def test_upstream_response_with_missing_fields(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    use_fake_upstream(monkeypatch, handler)
    response = client.get("/tools/convert", params=convert_params())

    assert response.status_code == 502
    assert response.json()["error"] == "invalid_upstream_response"
