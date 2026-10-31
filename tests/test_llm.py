import json

import httpx
import pytest

from raiseradar.db import DB
from raiseradar.llm import LLMClient, LLMQuotaExceeded, LLMUnavailable, extract_json


def completion(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def client_for(handler, models=("m1", "m2"), db=None, max_calls=20):
    return LLMClient("sk-test", list(models), cache=db, max_calls=max_calls, requests_per_minute=6000,
                     client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)


@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('Sure! Here you go:\n{"a": [1,2]}\nHope this helps', {"a": [1, 2]}),
    ('<think>hmm {"x":0}</think>{"a": 2}', {"a": 2}),
    ('[{"id": 1}]', [{"id": 1}]),
])
def test_extract_json(text, expected):
    assert extract_json(text) == expected


def test_extract_json_rejects_garbage():
    with pytest.raises(ValueError):
        extract_json("no json here")
    with pytest.raises(ValueError):
        extract_json("")


def test_happy_path_and_request_shape():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers["authorization"]
        return completion('{"ok": true}')

    c = client_for(handler)
    assert c.complete_json("sys", "usr") == {"ok": True}
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["model"] == "m1" and seen["body"]["temperature"] == 0
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert [m["role"] for m in seen["body"]["messages"]] == ["system", "user"]


def test_retries_without_json_mode_when_model_rejects_it():
    bodies = []

    def handler(req):
        body = json.loads(req.content)
        bodies.append(body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": "response_format unsupported"})
        return completion('{"ok": 1}')

    c = client_for(handler, models=["m1"])
    assert c.complete_json("s", "u") == {"ok": 1}
    assert len(bodies) == 2 and "response_format" not in bodies[1]
    c.complete_json("s", "u2")   # remembered: no json mode from now on
    assert "response_format" not in bodies[-1] and len(bodies) == 3


def test_falls_back_to_next_model_on_errors_and_bad_replies():
    calls = []

    def handler(req):
        model = json.loads(req.content)["model"]
        calls.append(model)
        if model == "m1":
            return httpx.Response(503, text="overloaded")
        if model == "m2":
            return completion("I cannot do that")
        return completion('{"ok": "m3"}')

    c = client_for(handler, models=["m1", "m2", "m3"])
    assert c.complete_json("s", "u") == {"ok": "m3"}
    assert calls == ["m1", "m2", "m3"] and c.model_used == {"m3": 1}


def test_rate_limit_is_retried_then_falls_back():
    n = {"m1": 0}

    def handler(req):
        model = json.loads(req.content)["model"]
        if model == "m1":
            n["m1"] += 1
            return httpx.Response(429, text="slow down", headers={"Retry-After": "1"})
        return completion('{"ok": 2}')

    c = client_for(handler)
    assert c.complete_json("s", "u") == {"ok": 2}
    assert n["m1"] == 2   # tried twice before moving on


def test_daily_limit_raises_quota_error():
    c = client_for(lambda r: httpx.Response(429, text='{"error":"Rate limit exceeded: free-models-per-day"}'))
    with pytest.raises(LLMQuotaExceeded):
        c.complete_json("s", "u")


def test_bad_key_and_missing_key():
    with pytest.raises(LLMQuotaExceeded):
        client_for(lambda r: httpx.Response(401, text="no")).complete_json("s", "u")
    c = LLMClient("", ["m1"], client=httpx.Client(transport=httpx.MockTransport(lambda r: completion("{}"))))
    with pytest.raises(LLMQuotaExceeded):
        c.complete_json("s", "u")


def test_404_model_is_skipped_for_the_rest_of_the_run():
    calls = []

    def handler(req):
        m = json.loads(req.content)["model"]
        calls.append(m)
        return httpx.Response(404, text="gone") if m == "m1" else completion('{"v": 1}')

    c = client_for(handler)
    c.complete_json("s", "u1"); c.complete_json("s", "u2")
    assert calls.count("m1") == 1


def test_all_models_failing_raises_unavailable():
    c = client_for(lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(LLMUnavailable):
        c.complete_json("s", "u")


def test_call_budget_is_enforced():
    c = client_for(lambda r: completion('{"v": 1}'), max_calls=2)
    c.complete_json("s", "a"); c.complete_json("s", "b")
    with pytest.raises(LLMQuotaExceeded):
        c.complete_json("s", "c")


def test_cache_avoids_repeat_calls(db):
    n = {"calls": 0}

    def handler(req):
        n["calls"] += 1
        return completion('{"v": 7}')

    c = client_for(handler, db=db)
    assert c.complete_json("s", "u") == {"v": 7}
    assert c.complete_json("s", "u") == {"v": 7}
    assert n["calls"] == 1 and c.cache_hits == 1


def test_validate_rejection_moves_to_next_model():
    def handler(req):
        return completion('{"items": []}') if json.loads(req.content)["model"] == "m1" else completion('{"items": [1]}')

    def validate(p):
        if not p["items"]:
            raise ValueError("empty")
        return True

    c = client_for(handler)
    assert c.complete_json("s", "u", validate=validate) == {"items": [1]}


def test_empty_content_and_error_payload_are_handled():
    replies = iter([httpx.Response(200, json={"choices": [{"message": {"content": None}}]}),
                    httpx.Response(200, json={"error": {"message": "provider down"}}),
                    completion('{"v": 3}')])
    c = client_for(lambda r: next(replies), models=["a", "b", "c"])
    assert c.complete_json("s", "u") == {"v": 3}


def test_optional_params_are_dropped_one_at_a_time_and_remembered():
    bodies = []

    def handler(req):
        body = json.loads(req.content)
        bodies.append(body)
        if "reasoning" in body:    # provider rejects both optional params
            return httpx.Response(400, json={"error": "unsupported parameter"})
        return completion('{"ok": 1}')

    c = client_for(handler, models=["m1"])
    c.reasoning_effort = "low"
    assert c.complete_json("s", "u") == {"ok": 1}
    assert "reasoning" in bodies[0] and bodies[0]["reasoning"] == {"effort": "low"} and "response_format" in bodies[0]
    assert "response_format" not in bodies[1] and "reasoning" in bodies[1]        # dropped first
    assert "reasoning" not in bodies[2] and len(bodies) == 3                       # then this
    c.complete_json("s", "u2")
    assert "reasoning" not in bodies[-1] and len(bodies) == 4                      # remembered for the model


def test_default_output_budget_is_large_enough_for_reasoning_models():
    seen = {}
    def handler(req):
        seen.update(json.loads(req.content))
        return completion('{"ok": 1}')
    client_for(handler).complete_json("s", "u")
    assert seen["max_tokens"] == 8000 and "reasoning" not in seen


def test_truncated_reply_is_salvaged_when_a_salvager_is_given():
    cut = '{"items": [{"id": 0, "v": 1}, {"id": 1, "v": 2}, {"id": 2, "v'
    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": cut}, "finish_reason": "length"}]})
    def salvage(text):
        return {"items": [1, 2]} if text.startswith('{"items"') else None
    c = client_for(handler, models=["m1"])
    assert c.complete_json("s", "u", salvage=salvage) == {"items": [1, 2]} and c.partial_replies == 1


def test_error_message_explains_why_a_reply_failed():
    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"items": [{"id": 0, "comp'}, "finish_reason": "length"}]})
    with pytest.raises(LLMUnavailable) as e:
        client_for(handler, models=["m1"]).complete_json("s", "u")
    msg = str(e.value)
    assert "finish_reason=length" in msg and "chars" in msg and "{\"items\"" in msg
