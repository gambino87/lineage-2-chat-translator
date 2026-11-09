"""Explicitly authorized, isolated no-model service checks; never part of build.py."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CACHE = ROOT / ".build-tools" / "secure-ollama"


def free_port(port: int) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


def request(port: int, path: str, key: str | None = None, body: dict | None = None, method: str | None = None) -> tuple[int, bytes]:
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=3) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def ready(process: subprocess.Popen, port: int, path: str, key: str | None = None) -> None:
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Isolated test service exited before becoming ready")
        try:
            status, _ = request(port, path, key)
            if status == 200:
                return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.1)
    raise RuntimeError("Isolated service readiness timeout")


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-isolated-service", action="store_true", required=True)
    parser.add_argument("--port", type=int, default=21435)
    parser.add_argument("--runner-port", type=int, default=21437)
    args = parser.parse_args()
    free_port(args.port)
    free_port(args.runner_port)
    scratch = ROOT / ".build" / "secure-ollama-smoke"
    scratch.mkdir(parents=True, exist_ok=True)
    trial = Path(tempfile.mkdtemp(prefix="no-model-", dir=scratch))
    runtime = trial / "runtime"
    runtime.mkdir()
    exe = runtime / "ollama.exe"
    shutil.copy2(CACHE / "output" / "ollama.exe", exe)
    archive = CACHE / "ollama-windows-amd64.zip"
    expected_archive = "65d68cade6cf6375f398b518224f8c8a15e062387f38cf477abfc848de7e7e29"
    with archive.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != expected_archive:
            raise RuntimeError("Official runtime archive changed")
    # Only core CPU libraries. No GPU backend, model, or personal runtime is used.
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            if item.filename.startswith("lib/ollama/") and item.filename.count("/") == 2 and not item.is_dir():
                bundle.extract(item, runtime)
    home = trial / "home"
    models = trial / "empty-models"
    home.mkdir()
    models.mkdir()
    key = secrets.token_hex(32)
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("OLLAMA_", "LLAMA_", "L2CT_"))}
    env.update(USERPROFILE=str(home), OLLAMA_HOST=f"127.0.0.1:{args.port}",
               OLLAMA_MODELS=str(models), OLLAMA_NO_CLOUD="1", OLLAMA_ORIGINS="",
               OLLAMA_DEBUG="0", OLLAMA_DEBUG_LOG_REQUESTS="0", L2CT_OLLAMA_API_KEY=key)
    checks = []
    log = trial / "frontend.log"
    with log.open("wb") as output:
        process = subprocess.Popen([str(exe), "serve"], cwd=runtime, env=env, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=output, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            ready(process, args.port, "/api/version", key)
            status, raw = request(args.port, "/api/version", key)
            assert status == 200 and json.loads(raw)["version"] == "0.40.14-l2ct.1"
            checks.append("authorized frontend version")
            for path, method, body in (("/api/version", "GET", None), ("/debug/pprof/", "GET", None),
                                       ("/api/delete", "DELETE", {"model": "synthetic-test"}),
                                       ("/api/pull", "POST", {"model": "synthetic-test"}),
                                       ("/api/create", "POST", {"model": "synthetic-test"})):
                for invalid in (None, "invalid", secrets.token_hex(32)):
                    status, _ = request(args.port, path, invalid, body, method)
                    assert status == 401, (path, status)
            checks.append("15 unauthorized frontend requests blocked")
            status, raw = request(args.port, "/api/tags", key)
            assert status == 200 and json.loads(raw).get("models") == []
            checks.append("empty model store unchanged")
        finally:
            stop(process)
    assert key.encode() not in log.read_bytes()
    checks.append("frontend session key absent from logs")
    for invalid in (None, "short"):
        bad_env = env.copy()
        if invalid is None:
            bad_env.pop("L2CT_OLLAMA_API_KEY")
        else:
            bad_env["L2CT_OLLAMA_API_KEY"] = invalid
        result = subprocess.run([str(exe), "serve"], cwd=runtime, env=bad_env, capture_output=True,
                                timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode != 0
        free_port(args.port)
    checks.append("missing and malformed frontend keys fail before listening")
    runner_key = secrets.token_hex(32)
    runner_env = env.copy()
    runner_env.update(LLAMA_API_KEY=runner_key)
    runner_env.pop("L2CT_OLLAMA_API_KEY", None)
    native_live_auth = "not exercised"
    runner_log = trial / "runner.log"
    with runner_log.open("wb") as output:
        runner = subprocess.Popen([str(runtime / "lib/ollama/llama-server.exe"), "--host", "127.0.0.1",
                                   "--port", str(args.runner_port), "--no-webui", "--offline",
                                   "--models-dir", str(models), "--no-models-autoload"],
                                  cwd=runtime, env=runner_env, stdin=subprocess.DEVNULL,
                                  stdout=output, stderr=output, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            try:
                ready(runner, args.runner_port, "/health")
            except RuntimeError:
                # The pinned Windows runner disables router subprocess support;
                # do not load a real model merely to complete this security check.
                output.flush()
                if b"subprocess is not enabled on this build" not in runner_log.read_bytes():
                    raise
                native_live_auth = "not exercised: official Windows runner cannot serve in no-model router mode"
            else:
                for path in ("/completion", "/tokenize", "/models/load", "/v1/chat/completions"):
                    status, _ = request(args.runner_port, path, None, {"prompt": "synthetic security test"})
                    assert status == 401, (path, status)
                checks.append("four unauthenticated native runner operations blocked")
                status, _ = request(args.runner_port, "/v1/models", runner_key)
                assert status == 200
                checks.append("authorized no-model native runner request")
                native_live_auth = "passed"
        finally:
            stop(runner)
    assert runner_key.encode() not in runner_log.read_bytes()
    checks.append("native runner key absent from logs")
    free_port(args.port)
    free_port(args.runner_port)
    checks.append("both isolated services stopped")
    report = {"result": "frontend checks passed", "checks": checks, "models_loaded": 0, "model_downloads": 0,
              "native_runner_live_auth": native_live_auth,
              "frontend_sha256": hashlib.sha256(exe.read_bytes()).hexdigest()}
    (CACHE / "output" / "smoke-validation.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
