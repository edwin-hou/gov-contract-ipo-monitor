"""One isolated Hermes-authenticated Codex request; no agent loop or fallback.

Run with the installed Hermes Python and ``-I``. Credentials stay in memory.
The ChatGPT Codex route rejects ``max_output_tokens``; that input bounds the
returned UTF-8 text locally, while the receipt reports actual provider usage.
The parent must reserve its call budget before starting this worker.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time

MODEL = "gpt-5.6-sol"
PROVIDER = "openai-codex"
BASE_URL = "https://chatgpt.com/backend-api/codex"
MAX_INPUT_BYTES = 65_536
MAX_STREAM_BYTES = 1_048_576
MAX_EVENT_BYTES = 262_144
MAX_EVENTS = 5_000
WALL_TIMEOUT_SECONDS = 90


class WorkerFailure(Exception):
    def __init__(self, code, *, diagnostics=None):
        self.code = code
        self.diagnostics = diagnostics
        super().__init__(code)


class DiscardDiagnostics:
    def write(self, text):
        return len(text)

    def flush(self):
        pass


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise WorkerFailure("invalid_json")
            result[key] = value
        return result

    def nonfinite(_):
        raise WorkerFailure("invalid_json")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)


def validate_input(raw):
    if len(raw) > MAX_INPUT_BYTES:
        raise WorkerFailure("input_too_large")
    try:
        value = strict_json(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError, WorkerFailure):
        raise WorkerFailure("invalid_json") from None
    expected = {"model", "reasoning_effort", "system", "payload", "max_output_tokens"}
    if not isinstance(value, dict) or set(value) != expected:
        raise WorkerFailure("invalid_input")
    if value["model"] != MODEL or value["reasoning_effort"] != "medium":
        raise WorkerFailure("unsupported_model_or_effort")
    if (not isinstance(value["system"], str) or not value["system"].strip()
            or len(value["system"].encode("utf-8")) > 12_000):
        raise WorkerFailure("invalid_system")
    if not isinstance(value["payload"], dict):
        raise WorkerFailure("invalid_payload")
    maximum = value["max_output_tokens"]
    if type(maximum) is not int or not 256 <= maximum <= 4096:
        raise WorkerFailure("invalid_output_limit")
    return value


def result(*, text="", usage=None, error_code=None, diagnostics=None):
    value = {"status": "unavailable" if error_code else "ok", "provider": PROVIDER,
             "model": MODEL, "text": text, "usage": usage or {}, "error_code": error_code}
    if diagnostics is not None:
        value["diagnostics"] = diagnostics
    return value


def prepare_request(value, preflight):
    # No tools, web browsing, previous-response replay, account data, or agent identity.
    request = {"model": MODEL, "instructions": value["system"],
               "input": [{"role": "user", "content": [{"type": "input_text",
                          "text": json.dumps(value["payload"], ensure_ascii=False,
                                             allow_nan=False, separators=(",", ":"))}]}],
               "store": False, "stream": True, "reasoning": {"effort": "medium"},
               "text": {"verbosity": "low"}, "prompt_cache_key": "ipo-analyst-v1"}
    return preflight(request, allow_stream=True, sanitize_harmony_tokens=True)


def sse_events(chunks, *, deadline, clock=time.monotonic):
    """Bound raw bytes before JSON parsing; require a real completed terminal event."""
    buffer, data = b"", []
    total = event_size = event_count = 0
    for chunk in chunks:
        if clock() >= deadline:
            raise WorkerFailure("wall_timeout")
        if not isinstance(chunk, bytes):
            raise WorkerFailure("invalid_stream")
        total += len(chunk)
        if total > MAX_STREAM_BYTES:
            raise WorkerFailure("stream_too_large")
        buffer += chunk
        if len(buffer) > MAX_EVENT_BYTES:
            raise WorkerFailure("stream_line_too_large")
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            line = line.removesuffix(b"\r")
            if line.startswith(b"data:"):
                part = line[5:]
                if part.startswith(b" "):
                    part = part[1:]
                data.append(part)
                event_size += len(part)
                if event_size > MAX_EVENT_BYTES:
                    raise WorkerFailure("stream_event_too_large")
            elif not line and data:
                raw = b"\n".join(data)
                data, event_size = [], 0
                if raw == b"[DONE]":
                    return
                event_count += 1
                if event_count > MAX_EVENTS:
                    raise WorkerFailure("too_many_events")
                try:
                    event = strict_json(raw.decode("utf-8"))
                except (ValueError, UnicodeError, RecursionError, WorkerFailure):
                    raise WorkerFailure("invalid_stream_json") from None
                if not isinstance(event, dict):
                    raise WorkerFailure("invalid_stream_event")
                yield event
    # Unflushed/truncated records are deliberately not promoted into a completion.


def completed_text(response, *, max_bytes):
    if not isinstance(response, dict) or response.get("status") != "completed":
        raise WorkerFailure("incomplete_response")
    if response.get("error") is not None:
        raise WorkerFailure("provider_response_failed")
    # Native Codex terminal frames may omit model metadata. The physical request
    # is already pinned to MODEL; an explicit different model remains a failure.
    if response.get("model") not in (None, MODEL):
        raise WorkerFailure("response_model_mismatch")
    output = response.get("output")
    if not isinstance(output, list) or not output:
        raise WorkerFailure("missing_output")
    parts = []
    for item in output:
        if not isinstance(item, dict):
            raise WorkerFailure("invalid_output")
        if item.get("type") == "reasoning":
            continue  # Hidden reasoning/encrypted content is never returned or persisted.
        if (item.get("type") != "message" or item.get("role") != "assistant"
                or item.get("status") != "completed"):
            raise WorkerFailure("unexpected_output_type")
        if item.get("phase") not in (None, "final_answer"):
            raise WorkerFailure("unexpected_message_phase")
        content = item.get("content")
        if not isinstance(content, list):
            raise WorkerFailure("invalid_output")
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                raise WorkerFailure("refused_or_unexpected_content")
            text = part.get("text")
            if not isinstance(text, str):
                raise WorkerFailure("invalid_output")
            parts.append(text)
    text = "\n".join(parts).strip()
    if not text:
        raise WorkerFailure("missing_output")
    if len(text.encode("utf-8")) > max_bytes:
        raise WorkerFailure("output_too_large")
    usage = response.get("usage")
    if not isinstance(usage, dict):
        raise WorkerFailure("missing_usage")
    safe_usage = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        count = usage.get(key)
        if type(count) is not int or not 0 <= count <= 2_000_000:
            raise WorkerFailure("invalid_usage")
        safe_usage[key] = count
    if safe_usage["total_tokens"] != safe_usage["input_tokens"] + safe_usage["output_tokens"]:
        raise WorkerFailure("invalid_usage")
    details = usage.get("output_tokens_details")
    if isinstance(details, dict):
        count = details.get("reasoning_tokens")
        if type(count) is int and 0 <= count <= safe_usage["output_tokens"]:
            safe_usage["reasoning_tokens"] = count
    return text, safe_usage


def _item_projection(item):
    """Validate done/terminal items and compare the authority-bearing fields."""
    if not isinstance(item, dict):
        raise WorkerFailure("invalid_output")
    kind = item.get("type")
    if kind == "reasoning":
        # Encrypted content and reasoning summaries cannot authorize a verdict.
        return {"type": kind, "id": item.get("id")}
    if (kind != "message" or item.get("role") != "assistant"
            or item.get("status") != "completed"):
        raise WorkerFailure("unexpected_output_type")
    if item.get("phase") not in (None, "final_answer"):
        raise WorkerFailure("unexpected_message_phase")
    content = item.get("content")
    if not isinstance(content, list) or not content:
        raise WorkerFailure("invalid_output")
    parts = []
    for part in content:
        if (not isinstance(part, dict) or part.get("type") != "output_text"
                or not isinstance(part.get("text"), str)):
            raise WorkerFailure("refused_or_unexpected_content")
        parts.append({"type": "output_text", "text": part["text"]})
    return {"type": kind, "id": item.get("id"), "role": "assistant",
            "status": "completed", "phase": item.get("phase"), "content": parts}


def consume_response(events, *, max_bytes):
    delta_bytes, done_items = 0, []
    done_ids, done_indexes, done_values = set(), set(), set()
    for event in events:
        kind = event.get("type")
        if isinstance(kind, str) and "_call" in kind:
            raise WorkerFailure("unexpected_output_type")
        if kind == "response.output_text.delta":
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise WorkerFailure("invalid_stream_event")
            delta_bytes += len(delta.encode("utf-8"))
            if delta_bytes > max_bytes:
                raise WorkerFailure("output_too_large")
        if kind in {"error", "response.failed", "response.incomplete", "response.cancelled",
                    "response.refusal.delta", "response.refusal.done"}:
            raise WorkerFailure("provider_response_failed")
        if kind == "response.output_item.added":
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") not in {"message", "reasoning"}:
                raise WorkerFailure("unexpected_output_type")
        if kind == "response.output_item.done":
            item = event.get("item")
            projection = _item_projection(item)
            identifier, index = item.get("id"), event.get("output_index")
            if identifier is not None and (not isinstance(identifier, str) or not identifier or len(identifier) > 200):
                raise WorkerFailure("invalid_output_identity")
            if index is not None and (type(index) is not int or not 0 <= index < MAX_EVENTS):
                raise WorkerFailure("invalid_output_identity")
            canonical = json.dumps(projection, sort_keys=True, ensure_ascii=False, allow_nan=False)
            if ((identifier is not None and identifier in done_ids)
                    or (index is not None and index in done_indexes) or canonical in done_values):
                raise WorkerFailure("duplicate_output_item")
            if identifier is not None:
                done_ids.add(identifier)
            if index is not None:
                done_indexes.add(index)
            done_values.add(canonical)
            done_items.append((index, item))
        if kind == "response.completed":
            response = event.get("response")
            if not isinstance(response, dict) or response.get("status") != "completed":
                raise WorkerFailure("incomplete_response")
            if response.get("model") not in (None, MODEL):
                raise WorkerFailure("response_model_mismatch")
            # Native Codex sends authoritative output_item.done before a terminal
            # frame containing only status/id/usage. Never promote text deltas or
            # merely announced items into a completed answer.
            if done_items:
                ordered = list(done_items)
                if all(index is not None for index, _ in ordered):
                    ordered.sort(key=lambda value: value[0])
                settled = [item for _, item in ordered]
                explicit = response.get("output")
                if explicit is not None:
                    if not isinstance(explicit, list) or ([_item_projection(item) for item in explicit]
                                                         != [_item_projection(item) for item in settled]):
                        raise WorkerFailure("conflicting_terminal_output")
                response = {**response, "output": settled}
            return completed_text(response, max_bytes=max_bytes)
    raise WorkerFailure("incomplete_stream")


def media_type_label(value):
    """Header values are diagnostic only and never copied into receipts."""
    if not isinstance(value, str) or not value.strip():
        return "missing"
    media = value.split(";", 1)[0].strip().lower()
    return {"text/event-stream": "sse", "application/json": "json",
            "text/plain": "plain", "text/html": "html"}.get(media, "other")


def bounded_chunks(chunks, *, deadline, clock=time.monotonic):
    total = 0
    for chunk in chunks:
        if clock() >= deadline:
            raise WorkerFailure("wall_timeout")
        if not isinstance(chunk, bytes):
            raise WorkerFailure("invalid_stream")
        total += len(chunk)
        if total > MAX_STREAM_BYTES:
            raise WorkerFailure("stream_too_large")
        yield chunk


def consume_http_body(chunks, *, deadline, max_bytes, diagnostics, clock=time.monotonic):
    """Recognize real protocol bytes, not an unreliable Content-Type label."""
    bounded = iter(bounded_chunks(chunks, deadline=deadline, clock=clock))
    prefix = b""
    for chunk in bounded:
        prefix += chunk
        start = prefix.removeprefix(b"\xef\xbb\xbf").lstrip()
        if not start:
            continue
        # Permit a chunk boundary inside the short SSE/HTML/JSON prefix.
        if len(start) < 8:
            continue
        break
    start = prefix.removeprefix(b"\xef\xbb\xbf").lstrip()
    if not start:
        diagnostics["body_protocol"] = "empty"
        raise WorkerFailure("empty_response")
    if start.startswith((b"data:", b"event:", b"id:", b"retry:", b":")):
        diagnostics["body_protocol"] = "sse"

        def replay():
            yield prefix.removeprefix(b"\xef\xbb\xbf")
            yield from bounded

        return consume_response(sse_events(replay(), deadline=deadline, clock=clock), max_bytes=max_bytes)
    if start[:1] in (b"{", b"["):
        diagnostics["body_protocol"] = "json"
        raw = bytearray(prefix.removeprefix(b"\xef\xbb\xbf"))
        for chunk in bounded:
            raw.extend(chunk)
        try:
            response = strict_json(raw.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError, WorkerFailure):
            raise WorkerFailure("invalid_response_json") from None
        if isinstance(response, dict) and response.get("error") is not None:
            raise WorkerFailure("provider_response_failed")
        if isinstance(response, dict) and "type" in response:
            if response["type"] != "response.completed":
                raise WorkerFailure("incomplete_response")
            response = response.get("response")
        return completed_text(response, max_bytes=max_bytes)
    diagnostics["body_protocol"] = "html" if start[:1] == b"<" else "unknown"
    raise WorkerFailure("invalid_body_protocol")


def _runtime():
    """Pin the installed Hermes auth route, not its mutable global model settings."""
    configured = os.environ.get("HERMES_HOME", "").strip()
    home = Path(configured).expanduser() if configured else Path.home() / "AppData" / "Local" / "hermes"
    if not home.is_absolute():
        raise WorkerFailure("hermes_profile_mismatch")
    home = home.resolve()
    expected = home / "hermes-agent" / "venv" / "Scripts" / "python.exe"
    if Path(sys.executable).resolve() != expected.resolve():
        raise WorkerFailure("hermes_interpreter_mismatch")
    source = home / "hermes-agent"
    if not (source / "hermes_cli" / "auth_codex.py").is_file():
        raise WorkerFailure("hermes_unavailable")
    os.environ["HERMES_HOME"] = str(home)
    # A job may not silently route credentials through a custom endpoint/proxy.
    os.environ.pop("HERMES_CODEX_BASE_URL", None)
    os.environ["HERMES_CODEX_REFRESH_TIMEOUT_SECONDS"] = "15"
    sys.path.insert(0, str(source))
    from hermes_cli.auth_codex import resolve_codex_runtime_credentials
    from agent.codex_headers import codex_cloudflare_headers
    from agent.codex_responses_adapter import _preflight_codex_api_kwargs
    import httpx

    # Read first: missing/invalid credentials fail instead of adopting another
    # program's credentials. Refresh the existing route only when it is expiring.
    credentials = resolve_codex_runtime_credentials(read_only=True)
    from hermes_cli.auth_codex import _codex_access_token_is_expiring
    if _codex_access_token_is_expiring(credentials.get("api_key"), 120):
        credentials = resolve_codex_runtime_credentials(refresh_if_expiring=True,
                                                        refresh_skew_seconds=120)
    if (credentials.get("provider") != PROVIDER or credentials.get("base_url") != BASE_URL
            or credentials.get("auth_mode") != "chatgpt"
            or not isinstance(credentials.get("api_key"), str) or not credentials["api_key"]):
        raise WorkerFailure("credential_route_mismatch")
    token = credentials["api_key"]
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json",
               "Accept": "text/event-stream", **codex_cloudflare_headers(token, base_url=BASE_URL)}
    client = httpx.Client(timeout=httpx.Timeout(connect=10, read=20, write=10, pool=5),
                          follow_redirects=False, trust_env=False)
    return client, headers, _preflight_codex_api_kwargs


def infer(value, *, runtime=_runtime, clock=time.monotonic):
    deadline = clock() + WALL_TIMEOUT_SECONDS
    client, headers, preflight = runtime()
    diagnostics = {"http_status": None, "media_type": "missing", "body_protocol": "unknown"}
    try:
        request = prepare_request(value, preflight)
        if clock() >= deadline:
            raise WorkerFailure("wall_timeout")
        # Exactly one physical POST. httpx has no default automatic request retry.
        with client.stream("POST", BASE_URL + "/responses", headers=headers, json=request) as response:
            diagnostics["http_status"] = response.status_code
            diagnostics["media_type"] = media_type_label(response.headers.get("content-type"))
            if response.status_code != 200:
                code = ("quota_or_rate_limit" if response.status_code == 429 else
                        "authentication_required" if response.status_code in (401, 403) else
                        "unsupported_request" if response.status_code == 400 else "provider_http_error")
                raise WorkerFailure(code)
            text, usage = consume_http_body(response.iter_bytes(chunk_size=4096), deadline=deadline,
                max_bytes=value["max_output_tokens"] * 6, diagnostics=diagnostics, clock=clock)
            return result(text=text, usage=usage, diagnostics=diagnostics)
    except WorkerFailure as exc:
        exc.diagnostics = diagnostics
        raise
    except BaseException:
        raise WorkerFailure("worker_unavailable", diagnostics=diagnostics) from None
    finally:
        client.close()


def run(raw, *, runtime=_runtime):
    try:
        value = validate_input(raw)
        return infer(value, runtime=runtime)
    except WorkerFailure as exc:
        return result(error_code=exc.code, diagnostics=exc.diagnostics)
    except BaseException:
        # Exception messages/HTTP bodies can contain credentials, prompts or source
        # text. Return a fixed code, never repr(), traceback or provider error body.
        return result(error_code="worker_unavailable")


def main():
    logging.disable(logging.CRITICAL)
    stdout, stderr = sys.stdout, sys.stderr
    emitted = False
    lock = threading.Lock()

    def emit(value):
        nonlocal emitted
        with lock:
            if emitted:
                return
            emitted = True
            stdout.write(json.dumps(value, ensure_ascii=True, allow_nan=False) + "\n")
            stdout.flush()

    def expire():
        emit(result(error_code="wall_timeout"))
        os._exit(124)  # Close this worker's sockets even if a read/import hangs.

    watchdog = threading.Timer(WALL_TIMEOUT_SECONDS, expire)
    watchdog.daemon = True
    watchdog.start()
    try:
        # Suppress dependency diagnostics as well as our own exceptions. Nothing
        # except the bounded JSON receipt may reach stdout/stderr.
        with contextlib.redirect_stdout(DiscardDiagnostics()), contextlib.redirect_stderr(DiscardDiagnostics()):
            raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
            value = run(raw)
        emit(value)
    except BaseException:
        emit(result(error_code="worker_unavailable"))
    finally:
        watchdog.cancel()
        sys.stdout, sys.stderr = stdout, stderr


if __name__ == "__main__":
    main()
