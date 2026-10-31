"""Public mode: a report meant for a public website shows excerpts of job descriptions, not the employers' full text."""
import json

from raiseradar import cli, report
from raiseradar.demo import build_demo_data
from raiseradar.report import excerpt, make_public


def test_excerpt_cuts_at_a_word_and_says_so():
    text = "alpha beta gamma delta epsilon zeta eta theta"
    cut, was_cut = excerpt(text, 20)
    assert was_cut and cut == "alpha beta gamma ..." and len(cut) <= 24
    assert excerpt("short", 20) == ("short", False)
    assert excerpt("anything at all", 0) == ("", True)                # 0 = link only, no text


def test_make_public_shortens_every_description_and_marks_it():
    data = make_public(build_demo_data(), 120)
    jobs = [j for c in data["companies"] for j in c["jobs"]]
    assert jobs and all(len(j["description"]) <= 125 for j in jobs)
    assert any(j["excerpt"] for j in jobs) and data["public"] == {"jd_chars": 120}


def test_public_report_is_noindex_and_the_normal_one_is_not(tmp_path):
    report.write_files(make_public(build_demo_data(), 120), tmp_path / "pub")
    report.write_files(build_demo_data(), tmp_path / "private")
    assert 'name="robots" content="noindex' in (tmp_path / "pub" / "index.html").read_text()
    assert "noindex" not in (tmp_path / "private" / "index.html").read_text()


def _long_descriptions(monkeypatch):
    def fake_build(p, tracker=False):
        d = build_demo_data()
        for c in d["companies"]:
            for j in c["jobs"]:
                j["description"] = "word " * 400
        return d
    monkeypatch.setattr(report, "build_data", fake_build)


def _longest(tmp_path):
    data = json.loads((tmp_path / "reports" / "data.json").read_text())
    return max(len(j["description"]) for c in data["companies"] for j in c["jobs"])


def test_report_command_flag_and_config_switch_public_mode(tmp_path, monkeypatch):
    _long_descriptions(monkeypatch)
    cfg = tmp_path / "config.toml"
    cfg.write_text("")
    cli.main(["-c", str(cfg), "report"])
    assert _longest(tmp_path) > 1500                                  # default: the full text, it is your own computer
    cli.main(["-c", str(cfg), "report", "--public"])
    assert _longest(tmp_path) <= 705                                  # default excerpt is 700 characters
    cfg.write_text("[report]\npublic = true\njd_excerpt_chars = 200\n")
    cli.main(["-c", str(cfg), "report"])                              # config alone is enough
    assert _longest(tmp_path) <= 205
