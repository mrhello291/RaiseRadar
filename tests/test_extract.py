import json

import httpx
import pytest

from companyfinder.extract import (extract_funding, format_items, normalise_round, parse_response,
                                   round_keyword_regex)
from companyfinder.llm import LLMClient
from companyfinder.models import Article


def art(i, title, pub="TechCrunch"):
    return Article(id=str(i), title=title, summary="", url=f"https://x/{i}", publisher=pub, published_at="2026-10-30T00:00:00+00:00", feed="f")


ARTS = [art(0, "Infisical raises $40M Series B led by Accel"), art(1, "Sequoia closes $950M fund"), art(2, "Zeta raises Rs 400 crore")]


def test_normalise_round():
    cases = {"Series B": "Series B", "series-b": "Series B", "SERIES C": "Series C", "Series F": "Series E+", "pre seed": "Pre-Seed",
             "Seed": "Seed", "growth equity": "Growth", "": "Unknown", None: "Unknown", "Bridge": "Other", "Series A": "Series A"}
    for raw, want in cases.items():
        assert normalise_round(raw) == want, raw


def test_round_keyword_regex():
    rx = round_keyword_regex(["Series B", "Series C"])
    for ok in ["raises Series B", "series-b round", "a $30M B round", "Series C funding", "SERIES  C"]:
        assert rx.search(ok), ok
    for bad in ["raises seed round", "Series A", "series bundle", "B2B startup raises", "Series D"]:
        assert not rx.search(bad), bad


def test_parse_response_handles_sloppy_output():
    parsed = {"items": [
        {"id": 0, "is_funding": True, "company": "Infisical", "round": "series b", "amount_usd_millions": "40",
         "amount_text": "$40M", "investors": "Accel, Y Combinator", "lead_investors": ["Accel"], "country": "United States",
         "city": None, "website": "null", "description": "secrets management"},
        {"id": "1", "is_funding": False, "company": "Sequoia"},
        {"id": 2, "is_funding": "true", "company": "Zeta Inc", "round": "Series C", "amount_usd_millions": 4.8},
        {"id": 99, "is_funding": True, "company": "Ghost"},         # unknown id -> ignored
        "garbage",
    ]}
    items, answered = parse_response(parsed, ARTS)
    assert answered == {0, 1, 2}
    assert [i.company for i in items] == ["Infisical", "Zeta Inc"]
    a = items[0]
    assert a.round == "Series B" and a.amount_usd_m == 40.0 and a.investors == ["Accel", "Y Combinator"]
    assert a.website is None and a.article is ARTS[0]
    assert items[1].amount_usd_m == 4.8


def test_parse_response_rejects_unusable_replies():
    with pytest.raises(ValueError):
        parse_response({"nothing": 1}, ARTS)
    with pytest.raises(ValueError):
        parse_response({"items": [{"id": 50, "is_funding": True, "company": "X"}]}, ARTS)


def test_funding_without_company_or_with_absurd_name_is_dropped():
    items, _ = parse_response({"items": [{"id": 0, "is_funding": True, "company": None},
                                         {"id": 1, "is_funding": True, "company": "x" * 200}]}, ARTS)
    assert items == []


def test_format_items_numbers_articles():
    text = format_items(ARTS)
    assert text.splitlines()[0].startswith("[0] Infisical raises $40M Series B led by Accel (via TechCrunch, 2026-10-30)")
    assert "[2]" in text


def test_extract_funding_end_to_end_with_mock_llm():
    reply = {"items": [{"id": 0, "is_funding": True, "company": "Infisical", "round": "Series B", "amount_usd_millions": 40,
                        "investors": ["Accel"], "lead_investors": ["Accel"]},
                       {"id": 1, "is_funding": False}, {"id": 2, "is_funding": False}]}
    seen = {}

    def handler(req):
        seen["prompt"] = json.loads(req.content)["messages"][1]["content"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "```json\n" + json.dumps(reply) + "\n```"}}]})

    llm = LLMClient("k", ["m"], requests_per_minute=6000, client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    items, answered = extract_funding(llm, ARTS)
    assert len(items) == 1 and items[0].company == "Infisical" and answered == {0, 1, 2}
    assert "[0] Infisical raises" in seen["prompt"] and "Never invent investors" in seen["prompt"]


# --- shapes seen from real free models --------------------------------------------------------------------------
from companyfinder.extract import salvage_items


def test_parse_response_accepts_other_wrappers():
    one = {"id": 0, "is_funding": True, "company": "Infisical", "round": "Series B"}
    for wrapper in ({"items": [one]}, [one], {"results": [one]}, {"data": {"items": [one]}}, {"0": {k: v for k, v in one.items() if k != "id"}}, one):
        items, answered = parse_response(wrapper, ARTS)
        assert [i.company for i in items] == ["Infisical"] and answered == {0}, wrapper


def test_parse_error_names_the_keys_it_saw():
    with pytest.raises(ValueError, match="keys: \\['answer', 'notes'\\]"):
        parse_response({"answer": "none", "notes": "x"}, ARTS)


def test_salvage_recovers_only_complete_items_from_a_cut_off_reply():
    cut = ('<think>hmm {"id": 9}</think>```json\n{"items": [{"id": 0, "is_funding": true, "company": "Infisical", "round": "Series B", "investors": ["Accel", "YC"]},'
           ' {"id": 1, "is_funding": false}, {"id": 2, "is_funding": true, "company": "Ze')
    got = salvage_items(cut)
    assert [i["id"] for i in got["items"]] == [0, 1]
    assert salvage_items("no json at all") is None and salvage_items("") is None


def test_extract_funding_keeps_partial_results_from_truncated_reply():
    cut = '{"items": [{"id": 0, "is_funding": true, "company": "Infisical", "round": "Series B"}, {"id": 1, "is_funding": false}, {"id": 2, "is_fun'
    llm = LLMClient("k", ["m"], requests_per_minute=6000, sleep=lambda s: None,
                    client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": cut}, "finish_reason": "length"}]}))))
    items, answered = extract_funding(llm, ARTS)
    assert [i.company for i in items] == ["Infisical"] and answered == {0, 1}      # article 2 was cut off -> not answered -> stays queued
    assert llm.partial_replies == 1
