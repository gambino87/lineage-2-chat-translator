# Private Windows Ollama backend

This directory maintains a narrow source patch against upstream Ollama
`v0.40.14`, commit `9a3e8177767245acc62b735eba7d5d8a553606d0`. It builds
`0.40.14-l2ct.1`. Only `ollama.exe` changes. Native llama.cpp, CPU/GPU DLLs,
and their notices remain byte-for-byte members of the SHA-256-pinned official
Windows archive. Model weights are unchanged.

## Transport contract

* The launcher generates a fresh 32-byte random key for each owned frontend
  process and passes its lowercase 64-character hex encoding in
  `L2CT_OLLAMA_API_KEY`. Do not place it in command arguments, settings, receipts,
  logs, source, or distribution files.
* Missing or malformed keys make `serve` fail before listening. The listener is
  restricted to the literal IPv4 loopback address `127.0.0.1`.
* Every frontend route, including `/api/version` and debug routes, requires
  `Authorization: Bearer <key>`. A wrapper authenticates before upstream handlers
  read request bodies. Public HTTP remains on loopback; this is not TLS.
* The Python caller must pin the **actual request connection**, before sending
  headers or bodies, to its retained owned frontend process identity. This is
  required: a separate preliminary port probe is insufficient.
* Every model runner gets a separate random `LLAMA_API_KEY` through its child
  environment. The upstream llama.cpp server already implements this API key.
  An inherited key-file setting is cleared. All Go runner RPCs use an
  authenticated transport that checks the actual established connection's
  reverse address/port tuple and owner PID with Windows `GetExtendedTcpTable`.
  It retains a live process handle, disallows proxies/redirects, and refuses
  uninitialized or exited runner identities before sending request bytes.
* Transient no-model GPU discovery servers are authenticated too. Model and
  discovery runners join the frontend's Windows kill-on-close Job Object.
* MLX models and the MLX runner entry point fail closed in this Windows build.
  Current translator GGUF model choices use llama.cpp and are unaffected.

This protects against another ordinary Windows account calling or impersonating
the private backend, provided the launcher keeps runtime files and process
credentials private to the installing account. It does not prevent the installing
user from reading, editing, or removing files. It does not claim protection from
malware already running as that user, administrators, kernel attackers, or all
denial-of-service attacks. Upstream llama.cpp intentionally leaves `/health`,
`/v1/health` and empty OPTIONS responses public; the web UI is disabled. Those
responses do not expose prompts or grant model-management/inference access.

## Build

Run from the project root on Windows x64:

```powershell
.build-tools/venv/Scripts/python.exe packaging/secure-ollama/build_secure_ollama.py
```

The script verifies pinned source, official runtime, Go 1.26.10 and LLVM-MinGW
20261104 archives; applies exact-context edits; runs nine focused Go security
tests; verifies downloaded Go modules against upstream's committed `go.sum`;
and builds the frontend with `-trimpath -buildvcs=false`. No model or service is
started. Tools and intermediates stay in `.build-tools/secure-ollama/`.

Outputs:

* `.build-tools/secure-ollama/output/ollama.exe`
* `.build-tools/secure-ollama/output/secure-ollama-manifest.json`

The final manifest contains `version`, `upstream_version`, `archive_sha256`,
`upstream_executable_sha256`, source provenance and `files`, each with `path`,
`sha256` and `bytes`. It describes all 75 runtime members, replacing only root
`ollama.exe` with the hardened frontend digest. The installer bundles that small
frontend under `_internal/secure-runtime/`; the official native archive remains
an installation-time download. The signed installer and embedded public manifest
establish trust; the installed mutable receipt does not establish trust.

Keep this patch, build lock and manifest public for transparency. Do not publish
the tool cache, models, session credentials or generated executable as source.
The existing upstream MIT license and native-library notices remain applicable;
this document does not add a license for original translator code.

## Validation

Focused Go tests cover missing/malformed credentials, authorization before route
execution, fresh random keys, fail-closed unstarted clients, real Windows socket
owner checks, rejection of a different process **without transmitting any HTTP
bytes**, redirect blocking, released identities, and malformed TCP tables.

An actual-service smoke check is separate from the normal build, requires explicit
launch permission, uses an empty isolated model/home directory and separate port,
and must never load a model or touch the running translator. Model inference and
GPU performance are not established by these security-only checks.

A separately authorized release validation also completed exactly one short
synthetic translation with the existing `qwen3.5:9b-q4_K_M` model. The original
manifest, blobs and metadata remained locked against writing/deletion; the model
store was unchanged afterward. The actual loaded native runner rejected three
unauthenticated operations with HTTP 401, and both owned processes stopped.
This establishes the tested end-to-end connection and shutdown path, not broad
translation quality or GPU performance across other systems.

## Upstream references

* [Pinned public listener and command](https://github.com/ollama/ollama/blob/9a3e8177767245acc62b735eba7d5d8a553606d0/cmd/cmd.go)
* [Pinned llama-server subprocess/RPC implementation](https://github.com/ollama/ollama/blob/9a3e8177767245acc62b735eba7d5d8a553606d0/llm/llama_server.go)
* [Pinned llama.cpp b11963 API authentication](https://github.com/ggml-org/llama.cpp/blob/b11963/tools/server/server-http.cpp)
* [Windows TCP owner table](https://learn.microsoft.com/en-us/windows/win32/api/iphlpapi/nf-iphlpapi-getextendedtcptable)
