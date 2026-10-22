import io
from unittest.mock import patch

import pytest

from deployment import transport_proxy as proxy


class Handler:
    command = "GET"
    headers = {}

    def __init__(self, path):
        self.path = path
        self.wfile = io.BytesIO()
        self.response_headers = {}

    def send_response(self, code):
        self.status = code

    def send_header(self, name, value):
        self.response_headers[name] = value

    def end_headers(self):
        pass


@pytest.fixture(autouse=True)
def clear_limits():
    proxy._requests.clear()
    proxy._per_ip.clear()


@pytest.mark.parametrize("path", ["/ulasim-api/redoc", "/ulasim-api/docs/../.env",
    "/ulasim-api/../api/canli", "/ulasim-api//example.org", "/ulasim-api/v1/lines/0",
    "/ulasim-api/v1/search?q=" + "x" * 4097, "/ulasim-api/download/../.env"])
def test_private_or_invalid_paths_never_reach_local_server(path):
    handler = Handler(path)
    with patch.object(proxy.http.client, "HTTPConnection") as conn:
        assert proxy.proxy_transport(handler)
        assert handler.status == 404
        conn.assert_not_called()


def test_other_website_paths_are_left_to_original_handler():
    assert proxy.proxy_transport(Handler("/api/canli")) is False
    assert proxy.proxy_transport(Handler("/ulasim-api-other")) is False


def test_valid_path_preserves_query_status_and_disables_cache():
    handler = Handler("/ulasim-api/v1/search?q=H%2F1&stops=false")
    handler.headers = {"X-API-Key": "test-key", "Authorization": "must-not-forward"}
    with patch.object(proxy.http.client, "HTTPConnection") as conn:
        response = conn.return_value.getresponse.return_value
        response.status = 503
        response.getheader.return_value = "application/json"
        response.read.return_value = b'{"error":{"code":"UPSTREAM_UNAVAILABLE"}}'
        assert proxy.proxy_transport(handler)
        conn.assert_called_once_with("127.0.0.1", 8000, timeout=25)
        conn.return_value.request.assert_called_once_with("GET", "/v1/search?q=H%2F1&stops=false",
            headers={"Accept": "application/json", "X-API-Key": "test-key"})
    assert handler.status == 503
    assert handler.response_headers["Cache-Control"] == "no-store"


def test_connection_failure_hides_internal_details():
    handler = Handler("/ulasim-api/healthz")
    with patch.object(proxy.http.client, "HTTPConnection") as conn:
        conn.return_value.request.side_effect = OSError("private details")
        proxy.proxy_transport(handler)
    assert handler.status == 502
    assert b"private details" not in handler.wfile.getvalue()


def test_request_limit_returns_retry_without_contacting_source():
    proxy._requests.extend([proxy.time.monotonic()] * proxy.TOTAL_PER_MINUTE)
    handler = Handler("/ulasim-api/healthz")
    with patch.object(proxy.http.client, "HTTPConnection") as conn:
        proxy.proxy_transport(handler)
        conn.assert_not_called()
    assert handler.status == 429
    assert handler.response_headers["Retry-After"] == "15"


def test_download_serves_only_the_fixed_apk_not_a_user_supplied_file(tmp_path):
    apk = tmp_path / "pilot.apk"
    apk.write_bytes(b"APK test content")
    handler = Handler("/ulasim-api/download")
    with patch.object(proxy, "APK_PATH", apk), patch.object(proxy.http.client, "HTTPConnection") as conn:
        assert proxy.proxy_transport(handler)
        conn.assert_not_called()
    assert handler.status == 200
    assert handler.wfile.getvalue() == b"APK test content"
    assert handler.response_headers["Content-Type"] == "application/vnd.android.package-archive"


def test_one_client_is_limited_without_blocking_others():
    with patch.object(proxy.http.client, "HTTPConnection") as conn:
        response = conn.return_value.getresponse.return_value
        response.status = 200
        response.getheader.return_value = "application/json"
        response.read.return_value = b"{}"
        for _ in range(proxy.IP_PER_MINUTE):
            noisy = Handler("/ulasim-api/healthz"); noisy.headers = {"CF-Connecting-IP": "203.0.113.5"}
            proxy.proxy_transport(noisy)
            assert noisy.status == 200
        noisy = Handler("/ulasim-api/healthz"); noisy.headers = {"CF-Connecting-IP": "203.0.113.5"}
        proxy.proxy_transport(noisy)
        other = Handler("/ulasim-api/healthz"); other.headers = {"CF-Connecting-IP": "198.51.100.7"}
        proxy.proxy_transport(other)
    assert noisy.status == 429
    assert other.status == 200


@pytest.mark.parametrize("path,kind", [("/ulasim-api/docs", "text/html"), ("/ulasim-api/", "text/html"),
    ("/ulasim-api/openapi.json", "application/json")])
def test_public_documents_are_served_without_local_api(path, kind):
    handler = Handler(path)
    with patch.object(proxy.http.client, "HTTPConnection") as conn:
        assert proxy.proxy_transport(handler)
        conn.assert_not_called()
    assert handler.status == 200
    assert handler.response_headers["Content-Type"].startswith(kind)
