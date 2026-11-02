import json
import ssl
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from dsh_trajectory.codec import DEFAULT_MAX_BYTES, strict_json
from dsh_trajectory.errors import TrajectoryError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPStore:
    def __init__(
        self,
        endpoint: str,
        *,
        token: str,
        ca_file: Path | None = None,
        allow_http: bool = False,
        timeout: float = 120,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ):
        parsed = urlparse(endpoint)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise TrajectoryError("Endpoint must be an HTTP(S) base URL without credentials/query")
        if parsed.scheme == "http" and not allow_http:
            raise TrajectoryError("HTTP requires --allow-http; use HTTPS for a remote center")
        if not token:
            raise TrajectoryError("Set DSH_TRAJECTORY_TOKEN for the ingestion service", code=4)
        if timeout <= 0:
            raise TrajectoryError("Timeout must be positive")
        self.endpoint = endpoint.rstrip("/")
        self.identity = "http:" + self.endpoint
        self.token = token
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.opener = build_opener(
            NoRedirect(),
            HTTPSHandler(
                context=ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
            ),
        )

    def request(self, path: str, payload: bytes | None = None, *, binary: bool = False):
        request = Request(
            self.endpoint + path,
            data=payload,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/zstd"},
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                content = response.read(self.max_bytes + 1)
                if len(content) > self.max_bytes:
                    raise TrajectoryError("Center response exceeds the configured byte limit")
                return content if binary else strict_json(content)
        except HTTPError as error:
            raise TrajectoryError(
                f"Center returned HTTP {error.code}; local data retained",
                code=4 if error.code in (401, 403) else 5 if error.code == 409 else 11,
                retryable=error.code >= 500 or error.code == 429,
            ) from error
        except (URLError, TimeoutError, ConnectionError) as error:
            raise TrajectoryError(
                "Cannot confirm center persistence; local data retained", code=11, retryable=True
            ) from error

    def ingest(self, payload: bytes) -> dict:
        receipt = self.request("/v1/bundles", payload)
        if not isinstance(receipt, dict):
            raise TrajectoryError("Center returned an invalid persistence receipt", code=11)
        return receipt


def make_server(store, host: str, port: int, token: str, max_bytes: int = DEFAULT_MAX_BYTES):
    import hmac
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlsplit

    if not token:
        raise TrajectoryError("Set DSH_TRAJECTORY_TOKEN before starting the center", code=4)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def reply(self, status: int, body, *, binary: bool = False):
            encoded = body if binary else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/zstd" if binary else "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(encoded)
            self.close_connection = True

        def authenticated(self) -> bool:
            if not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}"):
                self.reply(401, {"error": "Unauthorized"})
                return False
            return True

        def do_POST(self):
            if not self.authenticated():
                return
            if self.path != "/v1/bundles":
                self.reply(404, {"error": "Unknown route"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= max_bytes:
                    self.reply(413, {"error": "Invalid or excessive Content-Length"})
                    return
                payload = self.rfile.read(length)
                if len(payload) != length:
                    self.reply(400, {"error": "Incomplete body"})
                    return
                receipt = store.ingest(payload)
                self.reply(200, receipt)
            except TrajectoryError as error:
                self.reply(409 if error.code == 5 else 400, {"error": str(error)})
            except (ValueError, KeyError, TypeError):
                self.reply(400, {"error": "Malformed bundle"})
            except Exception:
                self.reply(503, {"error": "Persistence failed; retry with the same bundle"})

        def do_GET(self):
            if not self.authenticated():
                return
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            try:
                if parsed.path == "/v1/search":
                    self.reply(
                        200,
                        {
                            "results": store.search(
                                query["q"][0], int(query.get("limit", ["20"])[0])
                            )
                        },
                    )
                elif parsed.path == "/v1/bundles":
                    self.reply(
                        200, store.get(query["trial_id"][0], query["attempt_id"][0]), binary=True
                    )
                else:
                    self.reply(404, {"error": "Unknown route"})
            except (TrajectoryError, FileNotFoundError) as error:
                self.reply(404, {"error": str(error)})
            except (ValueError, KeyError, TypeError):
                self.reply(400, {"error": "Invalid query"})
            except Exception:
                self.reply(503, {"error": "Query failed"})

        def setup(self):
            self.request.settimeout(120)
            super().setup()

    return HTTPServer((host, port), Handler)
