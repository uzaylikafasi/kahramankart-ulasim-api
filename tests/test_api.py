import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from transport_api.main import create_app
from transport_api.provider import BASE_URL, Provider, ProviderError


def make_app(handler):
    return create_app(lambda: Provider(httpx.AsyncClient(
        base_url=BASE_URL, transport=httpx.MockTransport(handler))))


@pytest.mark.parametrize("path,operation,params", [
    ("/v1/search?q=H%2F1", "Search", {"SearchString": "H/1", "busStops": "1", "adress": "0", "busLines": "1"}),
    ("/v1/stops/nearby?latitude=37.58&longitude=36.92", "NearestStops", {"latitude": "37.58", "longitude": "36.92"}),
    ("/v1/stops/6968", "BusStop", {"stopId": "6968"}),
    ("/v1/stops/6968/lines", "StopLines", {"stopId": "6968"}),
    ("/v1/lines/9576?direction=2", "BusLine", {"lineId": "9576", "direction": "2"}),
    ("/v1/lines/9576/stops", "LineStop", {"lineId": "9576", "direction": "1"}),
    ("/v1/lines/9576/shape", "BusLinePoints", {"lineId": "9576", "direction": "1"}),
    ("/v1/timetables?line_code=H%2F1", "TimeTableAllDirection", {"lineCode": "H/1"}),
    ("/v1/announcements", "Announcements", {}),
])
def test_upstream_mapping_and_fields_are_preserved(path, operation, params):
    def handler(request):
        assert request.url.path == "/PalliumMarasServices/Service.svc/" + operation
        assert dict(request.url.params) == params
        return httpx.Response(200, json={"processInfo": 1, "newProviderField": {"value": 42}})

    with TestClient(make_app(handler)) as client:
        response = client.get(path)
        assert response.status_code == 200
        assert response.json()["data"]["newProviderField"] == {"value": 42}
        assert response.json()["meta"]["cached"] is False


def test_bounds_preserves_observed_provider_mapping():
    def handler(request):
        assert dict(request.url.params) == {
            "topLeftLatitude": "37.5", "topLeftLongitude": "37.0",
            "topRightLatitude": "37.7", "topRightLongitude": "37.0",
            "bottomLeftLatitude": "37.5", "bottomLeftLongitude": "36.8",
            "bottomRightLatitude": "37.7", "bottomRightLongitude": "36.8",
        }
        return httpx.Response(200, json={"busStops": [], "processInfo": "1"})

    with TestClient(make_app(handler)) as client:
        assert client.get("/v1/stops/in-bounds?south=37.5&west=36.8&north=37.7&east=37").status_code == 200


@pytest.mark.parametrize("path", [
    "/v1/stops/0", "/v1/lines/1?direction=4", "/v1/search?q=",
    "/v1/stops/nearby?latitude=91&longitude=36", "/v1/stops/nearby?latitude=nan&longitude=36",
    "/v1/stops/in-bounds?south=38&north=37&west=36&east=37",
    "/v1/search?q=x&stops=false&lines=false&addresses=false",
])
def test_bad_input_never_reaches_provider(path):
    def handler(request):
        pytest.fail("Invalid input reached upstream")

    with TestClient(make_app(handler)) as client:
        assert client.get(path).status_code == 422


@pytest.mark.parametrize("path,body", [
    ("/v1/lines/9576/vehicles", {"busLineInfo": {"liveBuses": []}, "processInfo": 1}),
    ("/v1/stops/6968/arrivals", {"buses": [], "processInfo": "1"}),
])
def test_empty_live_data_is_an_empty_list(path, body):
    with TestClient(make_app(lambda request: httpx.Response(200, json=body))) as client:
        assert client.get(path).json()["data"] == []


def test_missing_live_field_is_error_not_false_empty_result():
    with TestClient(make_app(lambda request: httpx.Response(200, json={"busLineInfo": {}}))) as client:
        result = client.get("/v1/lines/9576/vehicles")
        assert result.status_code == 502
        assert result.json()["error"]["code"] == "UPSTREAM_SCHEMA_CHANGED"


def test_geojson_order_and_cache_reuse():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"lineCoordinates": [
            {"latitude": 37.5, "longitude": 36.8}, {"latitude": 37.6, "longitude": 36.9}]})

    with TestClient(make_app(handler)) as client:
        client.get("/v1/lines/9576/shape")
        response = client.get("/v1/lines/9576/shape?format=geojson").json()
        assert response["data"]["geometry"]["coordinates"] == [[36.8, 37.5], [36.9, 37.6]]
        assert response["meta"]["cached"] is True
        assert len(calls) == 1


@pytest.mark.parametrize("status,body,expected", [
    (401, "secret upstream details", "UPSTREAM_AUTH_FAILED"),
    (429, "quota", "UPSTREAM_RATE_LIMITED"),
    (500, "server stack trace", "UPSTREAM_HTTP_ERROR"),
    (200, "<html>error</html>", "UPSTREAM_INVALID_RESPONSE"),
    (200, '{"processInfo":0}', "UPSTREAM_APPLICATION_ERROR"),
    (200, '{"Error":{"Code":1,"Detail":"secret"}}', "UPSTREAM_APPLICATION_ERROR"),
])
def test_upstream_failure_is_sanitized(status, body, expected):
    with TestClient(make_app(lambda request: httpx.Response(status, text=body))) as client:
        response = client.get("/v1/stops/6968")
        assert response.status_code in (502, 503)
        assert response.json()["error"]["code"] == expected
        assert "secret" not in response.text and "stack trace" not in response.text


def test_timeout_is_504():
    def handler(request):
        raise httpx.ReadTimeout("private details", request=request)

    with TestClient(make_app(handler)) as client:
        result = client.get("/v1/stops/6968")
        assert result.status_code == 504
        assert result.json()["error"]["code"] == "UPSTREAM_TIMEOUT"


def test_optional_key_and_cors(monkeypatch):
    monkeypatch.setenv("API_ACCESS_KEY", "local-test-key")
    with TestClient(make_app(lambda request: httpx.Response(200, json={}))) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/v1/capabilities").status_code == 401
        assert client.get("/v1/capabilities", headers={"X-API-Key": "local-test-key"}).status_code == 200
        response = client.options("/v1/search", headers={"Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET", "Access-Control-Request-Headers": "X-API-Key"})
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_journey_point_is_post_with_json_body():
    def handler(request):
        import json
        assert request.method == "POST"
        assert dict(request.url.params) == {}
        assert json.loads(request.content) == {"searchText": "otogar"}
        return httpx.Response(200, json={"GetJourneyPointResult": {"Data": [], "Error": {"Code": 0}}})

    with TestClient(make_app(handler)) as client:
        assert client.get("/v1/journey-points?q=otogar").status_code == 200


def test_journey_resolves_stop_coordinates_and_posts_the_observed_schema():
    def handler(request):
        import json
        if request.method == "GET":
            stop_id = request.url.params["stopId"]
            return httpx.Response(200, json={"location": {"latitude": 37.5 if stop_id == "9304" else 37.6,
                                                         "longitude": 36.9}, "processInfo": "1"})
        body = json.loads(request.content)
        assert body == {"modes": 1, "day": 3, "hour": "08", "minute": "05", "waiting": 20, "walking": 1000,
                        "latForFrom": 37.5, "lngForFrom": 36.9, "latForTo": 37.6, "lngForTo": 36.9,
                        "fromId": 9304, "toId": 6968}
        return httpx.Response(200, json={"data": [], "error": {"code": 0}})

    with TestClient(make_app(handler)) as client:
        response = client.get("/v1/journeys?from_stop_id=9304&to_stop_id=6968&day=3&hour=8&minute=5")
        assert response.status_code == 200
        assert response.json()["data"]["data"] == []


def test_nested_journey_error_is_not_reported_as_success():
    body = {"GetJourneyPointResult": {"Error": {"Code": 7, "Detail": "secret"}}}
    with TestClient(make_app(lambda request: httpx.Response(200, json=body))) as client:
        response = client.get("/v1/journey-points?q=otogar")
        assert response.status_code == 502
        assert response.json()["error"]["code"] == "UPSTREAM_APPLICATION_ERROR"


@pytest.mark.asyncio
async def test_concurrent_queries_share_one_upstream_request_and_cache_is_bounded():
    calls = []

    async def handler(request):
        calls.append(request)
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"processInfo": 1})

    provider = Provider(httpx.AsyncClient(base_url=BASE_URL, transport=httpx.MockTransport(handler)), max_entries=2)
    try:
        await asyncio.gather(*(provider.get("BusStop", {"stopId": 1}) for _ in range(10)))
        assert len(calls) == 1
        assert (await provider.get("BusStop", {"stopId": 1})).cached
        await provider.get("BusStop", {"stopId": 2})
        await provider.get("BusStop", {"stopId": 3})
        assert len(provider.cache) == 2
    finally:
        await provider.close()
