"""Protocol and failure-boundary tests; all provider calls are mocked."""
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


PATH = Path(__file__).resolve().parents[1] / "deploy" / "hermes" / "ipo_analyst_worker.py"
SPEC = importlib.util.spec_from_file_location("ipo_analyst_worker", PATH)
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


def request(**changes):
    value = {"model": worker.MODEL, "reasoning_effort": "medium", "system": "Use supplied evidence. Return JSON.",
             "payload": {"candidates": [{"symbol": "TEST", "evidence_ids": ["financial-1", "price-1"]}]},
             "max_output_tokens": 4096}
    return {**value, **changes}


def terminal(**changes):
    response = {"model": worker.MODEL, "status": "completed", "output": [
        {"type": "reasoning", "encrypted_content": "DO_NOT_RETURN", "summary": []},
        {"type": "message", "role": "assistant", "status": "completed", "content": [
            {"type": "output_text", "text": '{"decisions":[]}', "annotations": []}]}],
        "usage": {"input_tokens": 100, "output_tokens": 30, "total_tokens": 130,
                  "output_tokens_details": {"reasoning_tokens": 12}}}
    return {"type": "response.completed", "response": {**response, **changes}}


def stream(*events):
    raw = b"".join(b"data: " + json.dumps(event).encode() + b"\r\n\r\n" for event in events)
    # Exercise byte boundaries inside headers, JSON and UTF-8 sequences.
    return [raw[i:i + 7] for i in range(0, len(raw), 7)]


class Response:
    def __init__(self, chunks, status=200, content_type="text/event-stream"):
        self.chunks, self.status_code = chunks, status
        self.headers = {"content-type": content_type}
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def iter_bytes(self, **_):
        yield from self.chunks


class Client:
    def __init__(self, response=None, error=None):
        self.response, self.error = response, error
        self.calls, self.closed = [], False

    def stream(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        return self.response

    def close(self):
        self.closed = True


def runtime(client):
    return lambda: (client, {"Authorization": "Bearer SECRET"}, lambda value, **_: value)


def test_one_request_is_tools_free_medium_and_does_not_expose_credentials():
    client = Client(Response(stream(terminal())))
    answer = worker.run(json.dumps(request()).encode(), runtime=runtime(client))
    assert answer == {"status": "ok", "provider": "openai-codex", "model": worker.MODEL,
                      "text": '{"decisions":[]}', "usage": {"input_tokens": 100, "output_tokens": 30,
                      "total_tokens": 130, "reasoning_tokens": 12}, "error_code": None,
                      "diagnostics": {"http_status": 200, "media_type": "sse", "body_protocol": "sse"}}
    assert len(client.calls) == 1 and client.closed and client.response.closed
    args, kwargs = client.calls[0]
    assert args == ("POST", "https://chatgpt.com/backend-api/codex/responses")
    body = kwargs["json"]
    assert body["model"] == worker.MODEL and body["reasoning"] == {"effort": "medium"}
    assert body["store"] is False and body["stream"] is True
    assert not {"tools", "max_output_tokens", "previous_response_id", "temperature"}.intersection(body)
    assert "SECRET" not in json.dumps(answer) and "DO_NOT_RETURN" not in json.dumps(answer)


@pytest.mark.parametrize("changes", [{"model": "gpt-6-astra"}, {"model": "gpt-6.1-sol"},
                                     {"reasoning_effort": "xhigh"}, {"max_output_tokens": True},
                                     {"max_output_tokens": 4097}, {"payload": []}, {"system": ""},
                                     {"unexpected": "field"}])
def test_invalid_requests_do_not_resolve_credentials_or_make_a_call(changes):
    def forbidden():
        pytest.fail("invalid requests must not resolve auth")
    answer = worker.run(json.dumps(request(**changes)).encode(), runtime=forbidden)
    assert answer["status"] == "unavailable" and answer["text"] == ""


@pytest.mark.parametrize("raw", [b'{"model":"one","model":"two"}', b'{"x":NaN}', b'\xff', b'[' * 1500])
def test_strict_json_rejects_duplicate_nonfinite_utf8_and_recursion(raw):
    answer = worker.run(raw, runtime=lambda: pytest.fail("no auth for bad input"))
    assert answer["error_code"] == "invalid_json"


def test_oversized_input_does_not_resolve_auth():
    answer = worker.run(b"x" * (worker.MAX_INPUT_BYTES + 1), runtime=lambda: pytest.fail("no auth"))
    assert answer["error_code"] == "input_too_large"


@pytest.mark.parametrize("status,code", [(429, "quota_or_rate_limit"), (401, "authentication_required"),
                                       (403, "authentication_required"), (400, "unsupported_request"),
                                       (503, "provider_http_error"), (307, "provider_http_error")])
def test_http_failures_never_retry_or_read_private_error_bodies(status, code):
    client = Client(Response([b"secret account and prompt"], status=status))
    answer = worker.run(json.dumps(request()).encode(), runtime=runtime(client))
    assert answer["error_code"] == code and answer["text"] == ""
    assert len(client.calls) == 1 and client.closed
    assert "secret" not in json.dumps(answer)


def test_connection_exception_is_redacted_and_not_retried():
    client = Client(error=RuntimeError("Authorization: Bearer SECRET prompt SECRET"))
    answer = worker.run(json.dumps(request()).encode(), runtime=runtime(client))
    assert answer["error_code"] == "worker_unavailable"
    assert len(client.calls) == 1 and client.closed and "SECRET" not in json.dumps(answer)


@pytest.mark.parametrize("chunks,code", [
    (stream({"type": "response.output_text.delta", "delta": "partial"}), "incomplete_stream"),
    ([b"data: [DONE]\n\n"], "incomplete_stream"),
    (stream({"type": "response.failed", "error": {"message": "SECRET"}}), "provider_response_failed"),
    ([b"data: {\"broken\":NaN}\n\n"], "invalid_stream_json"),
    ([b"data: []\n\n"], "invalid_stream_event"),
    ([b"data: {}"], "incomplete_stream"),
])
def test_only_completed_response_can_authorize_output(chunks, code):
    client = Client(Response(chunks))
    answer = worker.run(json.dumps(request()).encode(), runtime=runtime(client))
    assert answer["error_code"] == code and answer["text"] == ""
    assert "SECRET" not in json.dumps(answer)


@pytest.mark.parametrize("changes,code", [
    ({"model": "gpt-6-astra"}, "response_model_mismatch"),
    ({"status": "incomplete"}, "incomplete_response"),
    ({"output": []}, "missing_output"),
    ({"output": [{"type": "function_call", "name": "place_order"}]}, "unexpected_output_type"),
    ({"output": [{"type": "message", "role": "assistant", "status": "completed", "content": [
        {"type": "refusal", "refusal": "SECRET"}]}]}, "refused_or_unexpected_content"),
    ({"usage": None}, "missing_usage"),
    ({"usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 99}}, "invalid_usage"),
    ({"usage": {"input_tokens": True, "output_tokens": 5, "total_tokens": 6}}, "invalid_usage"),
])
def test_terminal_model_refusal_tools_and_usage_are_validated(changes, code):
    answer = worker.run(json.dumps(request()).encode(), runtime=runtime(Client(Response(stream(terminal(**changes))))))
    assert answer["error_code"] == code and answer["text"] == ""


def test_output_limit_stops_stream_and_closes_connection():
    client = Client(Response(stream({"type": "response.output_text.delta", "delta": "x" * 1537}, terminal())))
    answer = worker.run(json.dumps(request(max_output_tokens=256)).encode(), runtime=runtime(client))
    assert answer["error_code"] == "output_too_large" and client.closed and client.response.closed


def test_raw_stream_and_line_event_limits(monkeypatch):
    monkeypatch.setattr(worker, "MAX_STREAM_BYTES", 10)
    with pytest.raises(worker.WorkerFailure, match="stream_too_large"):
        list(worker.sse_events([b"x" * 11], deadline=5, clock=lambda: 0))
    monkeypatch.setattr(worker, "MAX_STREAM_BYTES", 1000)
    monkeypatch.setattr(worker, "MAX_EVENT_BYTES", 10)
    with pytest.raises(worker.WorkerFailure, match="stream_line_too_large"):
        list(worker.sse_events([b"x" * 11], deadline=5, clock=lambda: 0))
    monkeypatch.setattr(worker, "MAX_EVENT_BYTES", 32)
    with pytest.raises(worker.WorkerFailure, match="stream_event_too_large"):
        list(worker.sse_events([b"data: 123456789012\n"] * 3, deadline=5, clock=lambda: 0))


def test_elapsed_wall_deadline_fails_closed():
    with pytest.raises(worker.WorkerFailure, match="wall_timeout"):
        list(worker.sse_events(stream(terminal()), deadline=5, clock=lambda: 5))


def native_done_item(*, identifier="msg_native", text='{"decisions":[]}', **changes):
    # Shape used by installed Hermes test_run_agent_codex_responses.py and
    # _CodexResponseAssembler: output_item.done precedes terminal metadata.
    return {"id": identifier, "type": "message", "role": "assistant", "status": "completed",
            "phase": "final_answer", "content": [{"type": "output_text", "text": text}], **changes}


def native_completion(*, output=None, **changes):
    response = {"id": "resp_native", "status": "completed", "output": output,
                "usage": {"input_tokens": 50, "output_tokens": 10, "total_tokens": 60}, **changes}
    return {"type": "response.completed", "response": response}


def native_answer(events):
    return worker.run(json.dumps(request()).encode(), runtime=runtime(Client(Response(stream(*events)))))


@pytest.mark.parametrize("model_fields", [{}, {"model": None}, {"model": worker.MODEL}])
def test_native_done_item_authorizes_terminal_without_output_or_model(model_fields):
    item = native_done_item()
    answer = native_answer([{"type": "response.output_item.done", "item": item, "output_index": 0},
                            native_completion(**model_fields)])
    assert answer["status"] == "ok" and answer["model"] == worker.MODEL
    assert answer["text"] == '{"decisions":[]}' and answer["usage"]["total_tokens"] == 60


def test_omitted_terminal_output_key_uses_authoritative_done_item():
    terminal_event = native_completion()
    del terminal_event["response"]["output"]
    answer = native_answer([{"type": "response.output_item.done", "item": native_done_item()}, terminal_event])
    assert answer["status"] == "ok"


def test_terminal_only_full_output_can_omit_model_metadata():
    answer = native_answer([native_completion(output=[native_done_item()])])
    assert answer["status"] == "ok" and answer["model"] == worker.MODEL


def test_matching_explicit_terminal_output_and_done_item_are_accepted():
    item = native_done_item()
    answer = native_answer([{"type": "response.output_item.done", "item": item, "output_index": 0},
                            native_completion(output=[item], model=worker.MODEL)])
    assert answer["status"] == "ok"


def test_done_items_sort_by_explicit_output_index():
    first = native_done_item(identifier="msg_0", text='{"decisions":[')
    second = native_done_item(identifier="msg_1", text=']}' )
    answer = native_answer([{"type": "response.output_item.done", "item": second, "output_index": 1},
                            {"type": "response.output_item.done", "item": first, "output_index": 0},
                            native_completion(output=[first, second])])
    assert answer["status"] == "ok" and json.loads(answer["text"]) == {"decisions": []}


@pytest.mark.parametrize("extra", [{"model": "gpt-6-astra"}, {"model": "gpt-5.6-sol-unverified-alias"},
                                  {"model": ""}])
def test_explicit_model_conflict_is_rejected_even_with_completed_done_item(extra):
    answer = native_answer([{"type": "response.output_item.done", "item": native_done_item()}, native_completion(**extra)])
    assert answer["error_code"] == "response_model_mismatch" and answer["text"] == ""


@pytest.mark.parametrize("changes", [{"status": "in_progress"}, {"status": "incomplete"},
                                    {"role": "user"}, {"phase": "analysis"}, {"phase": "commentary"},
                                    {"content": [{"type": "refusal", "refusal": "SECRET"}]}])
def test_incomplete_nonfinal_or_refused_done_item_cannot_authorize_terminal(changes):
    answer = native_answer([{"type": "response.output_item.done", "item": native_done_item(**changes)},
                            native_completion(output=[native_done_item()])])
    assert answer["status"] == "unavailable" and answer["text"] == ""
    assert "SECRET" not in json.dumps(answer)


@pytest.mark.parametrize("second,index", [(native_done_item(), 1),
                                         (native_done_item(text="conflicting JSON"), 1),
                                         (native_done_item(identifier="another_id"), 0)])
def test_duplicate_or_conflicting_done_identity_is_rejected(second, index):
    answer = native_answer([{"type": "response.output_item.done", "item": native_done_item(), "output_index": 0},
                            {"type": "response.output_item.done", "item": second, "output_index": index},
                            native_completion()])
    assert answer["error_code"] == "duplicate_output_item" and answer["text"] == ""


def test_duplicate_unidentified_done_items_are_rejected():
    item = native_done_item()
    del item["id"]
    done = {"type": "response.output_item.done", "item": item}
    answer = native_answer([done, done, native_completion()])
    assert answer["error_code"] == "duplicate_output_item"


@pytest.mark.parametrize("output", [[], [native_done_item(text="conflicting JSON")],
                                    [native_done_item(identifier="conflicting_id")], "not-an-output-list"])
def test_conflicting_explicit_terminal_output_is_rejected(output):
    answer = native_answer([{"type": "response.output_item.done", "item": native_done_item()},
                            native_completion(output=output)])
    assert answer["error_code"] == "conflicting_terminal_output" and answer["text"] == ""


@pytest.mark.parametrize("events", [
    [{"type": "response.output_item.done", "item": native_done_item()}],
    [{"type": "response.output_text.delta", "delta": '{"decisions":[]}'}, native_completion()],
    [{"type": "response.output_text.done", "text": '{"decisions":[]}'}, native_completion()],
    [{"type": "response.output_item.added", "item": native_done_item(status="in_progress")}, native_completion()],
])
def test_done_or_delta_or_announced_content_alone_never_becomes_a_verdict(events):
    answer = native_answer(events)
    assert answer["status"] == "unavailable" and answer["text"] == ""


@pytest.mark.parametrize("event", [
    {"type": "response.refusal.delta", "delta": "SECRET"},
    {"type": "response.function_call_arguments.delta", "delta": "SECRET"},
    {"type": "response.web_search_call.completed"},
    {"type": "response.output_item.added", "item": {"type": "function_call", "name": "place_order"}},
    {"type": "response.output_item.done", "item": {"type": "function_call", "name": "place_order"}},
])
def test_tool_or_refusal_events_fail_closed_even_if_terminal_contains_valid_text(event):
    answer = native_answer([event, native_completion(output=[native_done_item()])])
    assert answer["status"] == "unavailable" and answer["text"] == ""
    assert "SECRET" not in json.dumps(answer)


@pytest.mark.parametrize("media,label", [("text/plain", "plain"), (None, "missing"),
                                        ("", "missing"), ("application/json", "json"),
                                        ("vendor/SECRET", "other")])
def test_valid_sse_uses_protocol_despite_missing_or_non_sse_media_label(media, label):
    item = native_done_item()
    response = Response(stream({"type": "response.output_item.done", "item": item}, native_completion()), content_type=media)
    client = Client(response)
    answer = worker.run(json.dumps(request()).encode(), runtime=runtime(client))
    assert answer["status"] == "ok" and answer["text"] == '{"decisions":[]}'
    assert answer["diagnostics"] == {"http_status": 200, "media_type": label, "body_protocol": "sse"}
    assert "SECRET" not in json.dumps(answer) and len(client.calls) == 1 and client.closed


def test_valid_sse_without_content_type_header():
    response = Response(stream(terminal()))
    response.headers = {}
    answer = worker.run(json.dumps(request()).encode(), runtime=runtime(Client(response)))
    assert answer["status"] == "ok" and answer["diagnostics"]["media_type"] == "missing"


@pytest.mark.parametrize("media", ["application/json", "application/json; charset=utf-8", "text/plain", None])
def test_complete_responses_json_is_validated_without_inference_retry(media):
    raw = json.dumps(terminal()["response"]).encode()
    response = Response([raw[i:i + 3] for i in range(0, len(raw), 3)], content_type=media)
    client = Client(response)
    answer = worker.run(json.dumps(request()).encode(), runtime=runtime(client))
    assert answer["status"] == "ok" and answer["text"] == '{"decisions":[]}'
    assert answer["diagnostics"]["body_protocol"] == "json"
    assert answer["usage"]["total_tokens"] == 130
    assert "DO_NOT_RETURN" not in json.dumps(answer) and len(client.calls) == 1 and client.closed


@pytest.mark.parametrize("raw,protocol,code", [
    (b"<!DOCTYPE html><html>SECRET gateway page</html>", "html", "invalid_body_protocol"),
    (b"<html>SECRET login</html>", "html", "invalid_body_protocol"),
    (b"SECRET plain error page", "unknown", "invalid_body_protocol"),
    (b" \r\n\t", "empty", "empty_response"),
    (b'{"status":"completed","error":{"message":"SECRET"}}', "json", "provider_response_failed"),
    (b'{"status":"in_progress","output_text":"SECRET"}', "json", "incomplete_response"),
    (b'{"type":"response.output_text.delta","delta":"SECRET"}', "json", "incomplete_response"),
    (b'{"status":"completed",', "json", "invalid_response_json"),
    (b'{"status":"wait","status":"completed"}', "json", "invalid_response_json"),
    (b'{"status":NaN}', "json", "invalid_response_json"),
])
def test_html_error_incomplete_and_delta_only_bodies_are_rejected_and_redacted(raw, protocol, code):
    response = Response([raw], content_type="application/json; SECRET")
    client = Client(response)
    answer = worker.run(json.dumps(request()).encode(), runtime=runtime(client))
    assert answer["status"] == "unavailable" and answer["error_code"] == code and answer["text"] == ""
    assert answer["diagnostics"] == {"http_status": 200, "media_type": "json", "body_protocol": protocol}
    assert "SECRET" not in json.dumps(answer) and len(client.calls) == 1 and client.closed


@pytest.mark.parametrize("changes", [{"model": "gpt-6-astra"}, {"status": "incomplete"},
                                    {"output": [{"type": "function_call", "name": "place_order"}]},
                                    {"output": [{"type": "message", "role": "user", "status": "completed", "content": []}]},
                                    {"usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 99}}])
def test_json_response_retains_model_status_role_tools_and_usage_checks(changes):
    raw = json.dumps(terminal(**changes)["response"]).encode()
    answer = worker.run(json.dumps(request()).encode(), runtime=runtime(Client(Response([raw], content_type="application/json"))))
    assert answer["status"] == "unavailable" and answer["text"] == ""
    assert answer["diagnostics"]["body_protocol"] == "json"


@pytest.mark.parametrize("media", ["application/json", "text/plain", None])
def test_complete_json_terminal_envelope_is_authoritative(media):
    raw = json.dumps(terminal()).encode()
    client = Client(Response([raw], content_type=media))
    answer = worker.run(json.dumps(request()).encode(), runtime=runtime(client))
    assert answer["status"] == "ok" and answer["text"] == '{"decisions":[]}'
    assert answer["diagnostics"]["body_protocol"] == "json" and len(client.calls) == 1


@pytest.mark.parametrize("event", [
    {"type": "response.output_item.done", "item": native_done_item()},
    {"type": "response.output_text.done", "text": '{"decisions":[]}'},
    native_completion(),
    native_completion(output=[native_done_item()], status="incomplete"),
    native_completion(output=[native_done_item()], model="gpt-6-astra"),
])
def test_json_done_only_or_incomplete_terminal_envelope_cannot_authorize(event):
    answer = worker.run(json.dumps(request()).encode(),
                        runtime=runtime(Client(Response([json.dumps(event).encode()], content_type="application/json"))))
    assert answer["status"] == "unavailable" and answer["text"] == ""


@pytest.mark.parametrize("event_type", ["response.output_text.delta", "response.output_item.done", "response.output_text.done"])
def test_json_nonterminal_event_cannot_borrow_completed_response_fields(event_type):
    event = {**terminal()["response"], "type": event_type}
    answer = worker.run(json.dumps(request()).encode(),
                        runtime=runtime(Client(Response([json.dumps(event).encode()], content_type="application/json"))))
    assert answer["error_code"] == "incomplete_response" and answer["text"] == ""


def test_json_response_raw_limit_is_applied_before_parsing(monkeypatch):
    monkeypatch.setattr(worker, "MAX_STREAM_BYTES", 32)
    response = Response([b'{"status":"completed",', b'"SECRET":"' + b"x" * 33], content_type="application/json")
    answer = worker.run(json.dumps(request()).encode(), runtime=runtime(Client(response)))
    assert answer["error_code"] == "stream_too_large" and answer["text"] == ""
    assert "SECRET" not in json.dumps(answer)


def test_json_response_output_limit_and_wall_deadline_remain_enforced():
    response = terminal()["response"]
    response["output"][-1]["content"][0]["text"] = "x" * 1537
    answer = worker.run(json.dumps(request(max_output_tokens=256)).encode(),
                        runtime=runtime(Client(Response([json.dumps(response).encode()], content_type="application/json"))))
    assert answer["error_code"] == "output_too_large"
    diagnostics = {"http_status": 200, "media_type": "json", "body_protocol": "unknown"}
    with pytest.raises(worker.WorkerFailure, match="wall_timeout"):
        worker.consume_http_body([b"{}"], deadline=5, max_bytes=24576, diagnostics=diagnostics, clock=lambda: 5)


def test_main_emits_only_json_and_suppresses_dependency_diagnostics(monkeypatch):
    original_out, original_err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", original_out)
    monkeypatch.setattr(sys, "stderr", original_err)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(request()).encode())))
    monkeypatch.setattr(worker.logging, "disable", lambda _: None)

    def noisy_run(raw):
        print("SECRET")
        print("SECRET", file=sys.stderr)
        return worker.result(error_code="worker_unavailable")

    monkeypatch.setattr(worker, "run", noisy_run)
    worker.main()
    value = json.loads(original_out.getvalue())
    assert value["error_code"] == "worker_unavailable"
    assert "SECRET" not in original_out.getvalue() and original_err.getvalue() == ""


def test_worker_rejects_relative_profile_before_reading_auth(monkeypatch):
    monkeypatch.setenv('HERMES_HOME','relative-profile')
    with pytest.raises(worker.WorkerFailure,match='hermes_profile_mismatch'):
        worker._runtime()


def test_worker_requires_owning_profile_interpreter_before_auth(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    with pytest.raises(worker.WorkerFailure,match='hermes_interpreter_mismatch'):
        worker._runtime()
