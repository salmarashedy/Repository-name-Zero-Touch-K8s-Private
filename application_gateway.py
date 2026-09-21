"""Signed, authenticated HTTP gateway for internal Kubernetes Services."""

from __future__ import annotations

import atexit
import os
import socket
import subprocess
import threading
import time

import requests
from flask import Response, abort, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

_lock = threading.Lock()
_forwards: dict[tuple[str, str, str, int], tuple[subprocess.Popen, int, float]] = {}


def _serializer(secret_key: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key, salt="zero-touch-application-gateway")


def create_application_token(secret_key: str, cluster: str, namespace: str, service: str, port: int) -> str:
    return _serializer(secret_key).dumps({
        "cluster": cluster, "namespace": namespace, "service": service, "port": int(port)
    })


def read_application_token(secret_key: str, token: str) -> dict:
    try:
        return _serializer(secret_key).loads(
            token, max_age=int(os.environ.get("GATEWAY_TOKEN_MAX_AGE", 7 * 24 * 3600))
        )
    except (BadSignature, SignatureExpired):
        abort(403, "This application link is invalid or has expired.")


def _available_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _local_port(cluster: str, namespace: str, service: str, service_port: int) -> int:
    key = (cluster, namespace, service, service_port)
    with _lock:
        existing = _forwards.get(key)
        if existing and existing[0].poll() is None:
            _forwards[key] = (existing[0], existing[1], time.time())
            return existing[1]
        port = _available_port()
        process = subprocess.Popen(
            ["kubectl", "--context", cluster, "-n", namespace, "port-forward",
             f"service/{service}", f"{port}:{service_port}", "--address", "127.0.0.1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            if process.poll() is not None:
                raise RuntimeError("The application gateway could not reach the Kubernetes Service.")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.2):
                    _forwards[key] = (process, port, time.time())
                    return port
            except OSError:
                time.sleep(.15)
        process.terminate()
        raise RuntimeError("The application gateway connection timed out.")


def proxy_application(secret_key: str, token: str, subpath: str = "") -> Response:
    target = read_application_token(secret_key, token)
    port = _local_port(target["cluster"], target["namespace"], target["service"], target["port"])
    excluded = {"host", "content-length", "connection"}
    headers = {key: value for key, value in request.headers if key.lower() not in excluded}
    response = requests.request(
        request.method, f"http://127.0.0.1:{port}/{subpath}", params=request.args,
        data=request.get_data(), headers=headers, allow_redirects=False, timeout=60,
    )
    returned_headers = [(key, value) for key, value in response.headers.items()
                        if key.lower() not in {"content-encoding", "transfer-encoding", "connection", "content-length"}]
    return Response(response.content, response.status_code, returned_headers)


@atexit.register
def _stop_forwards() -> None:
    for process, _, _ in list(_forwards.values()):
        if process.poll() is None:
            process.terminate()
