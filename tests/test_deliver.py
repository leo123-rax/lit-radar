"""Delivery: HTML sanitiser for the e-mail body, verified-TLS SMTP, failure handling; the zero-credential
channels (GitHub Issue through a mocked `gh`, Atom feed), the `deliver` command, and the workflow wiring."""
import json
import re
import smtplib
import ssl
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest
import yaml

from litradar import cli
from litradar.config import REPO_ROOT, Settings
from litradar.digest import deliver


# ---- sanitiser ---------------------------------------------------------------------------------
def test_sanitize_keeps_allow_listed_tags_and_drops_everything_else():
    raw = (
        '<h2 class="x">Top</h2><p onclick="evil()">Hello <b>bold</b> <em>em</em> <sup>1</sup></p>'
        '<script>alert(1)</script><style>p{}</style><iframe src="https://evil.example"></iframe>'
        '<table><thead><tr><th>a</th></tr></thead><tbody><tr><td>1</td></tr></tbody></table>'
        '<a href="https://example.org/paper" target="_blank" onmouseover="x()">link</a>'
        '<a href="javascript:alert(1)">bad</a><img src="x" onerror="y()"><div><span>text</span></div><br/>'
        "<!-- comment -->"
    )
    out = deliver.sanitize_html(raw)
    assert out.startswith("<h2>Top</h2><p>Hello <b>bold</b> <em>em</em> <sup>1</sup></p>")
    assert "<script" not in out and "alert(1)" not in out and "<style" not in out and "p{}" not in out and "<iframe" not in out
    assert "onclick" not in out and "onmouseover" not in out and "target=" not in out and "onerror" not in out
    assert '<a href="https://example.org/paper">link</a>' in out
    assert "<a>bad</a>" in out and "javascript:" not in out
    assert "<img" not in out and "<div>" not in out and "<span>" not in out and "text" in out
    assert "<br>" in out and "comment" not in out
    assert "<table><thead><tr><th>a</th></tr></thead><tbody><tr><td>1</td></tr></tbody></table>" in out


def test_sanitize_is_case_insensitive_and_keeps_text_entities():
    out = deliver.sanitize_html("<SCRIPT>x</SCRIPT><P>A &amp; B &lt;tag&gt;</P><STRONG>s</STRONG>")
    assert out == "<p>A &amp; B &lt;tag&gt;</p><strong>s</strong>"


def test_markdown_to_html_without_markdown_package_escapes_everything(monkeypatch):
    monkeypatch.setitem(sys.modules, "markdown", None)  # makes `import markdown` raise ImportError
    out = deliver.markdown_to_html("# Title <script>alert(1)</script> & co | a |")
    assert out.startswith("<pre") and "<script" not in out and "&lt;script&gt;" in out and "&amp; co" in out


def test_markdown_to_html_with_markdown_package_is_sanitised():
    pytest.importorskip("markdown")
    md = "| a | b |\n|---|---|\n| 1 | <script>alert(1)</script> |\n\n[x](https://example.org) <img src=x onerror=y()>"
    out = deliver.markdown_to_html(md)
    assert "<table>" in out and "<td>1</td>" in out
    assert "<script" not in out and "<img" not in out and "onerror" not in out
    assert '<a href="https://example.org">x</a>' in out


# ---- SMTP ---------------------------------------------------------------------------------------
class _FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.starttls_context = None
        self.logged_in = None
        self.sent = []
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.starttls_context = context

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, msg):
        self.sent.append(msg)


def _settings(port=587):
    return Settings(smtp_host="smtp.example.org", smtp_port=port, smtp_user="u", smtp_password="p", digest_from="a@example.org", digest_to="b@example.org, c@example.org")


def test_starttls_uses_a_verifying_default_context(monkeypatch):
    _FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    assert deliver.send_email(_settings(587), "subj", "# hi\n\nbody") is True
    s = _FakeSMTP.instances[0]
    assert isinstance(s.starttls_context, ssl.SSLContext)
    assert s.starttls_context.verify_mode == ssl.CERT_REQUIRED and s.starttls_context.check_hostname is True
    assert s.logged_in == ("u", "p") and len(s.sent) == 1
    assert s.sent[0]["To"] == "b@example.org, c@example.org"


def test_port_465_uses_smtp_ssl_with_the_same_context(monkeypatch):
    _FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP", None)  # must not be used
    assert deliver.send_email(_settings(465), "subj", "body") is True
    s = _FakeSMTP.instances[0]
    assert isinstance(s.context, ssl.SSLContext) and s.context.verify_mode == ssl.CERT_REQUIRED
    assert s.starttls_context is None and len(s.sent) == 1


def test_smtp_failure_is_logged_and_returns_false(monkeypatch, caplog):
    class Boom(_FakeSMTP):
        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(smtplib, "SMTP", Boom)
    assert deliver.send_email(_settings(), "subj", "body") is False
    assert "e-mail delivery failed" in caplog.text


def test_unconfigured_email_returns_false():
    assert deliver.send_email(Settings(), "subj", "body") is False


# ---- zero-credential channels: GitHub Issue + Atom feed ----------------------------------------
ATOM = "{http://www.w3.org/2005/Atom}"
RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
LINKS = deliver.RepoLinks(repo="Owner/radar", branch="main")


def _digest(date, lines=40, rendered=None):
    head = f"# Science literature radar: 2026-01-01 to {date}\n\n100 new papers in the window.\n\n"
    if rendered:
        head += f"_Rendered {rendered} UTC; every count is a snapshot._\n\n"
    body = "\n".join(f"**{i}. [Paper {i}](https://doi.org/10.1/{i})**  \nAbstract <b>{i}</b> & more  " for i in range(lines))
    return head + "## Top innovations\n\n" + body + "\n"


@pytest.fixture
def digests(tmp_path):
    d = tmp_path / "digests"
    d.mkdir()
    (d / "2026-10-04.md").write_text(_digest("2026-10-04"), encoding="utf-8")
    (d / "2026-10-11.md").write_text(_digest("2026-10-11", rendered="2026-10-11 01:05"), encoding="utf-8")
    (d / "2026-10-18.md").write_text(_digest("2026-10-18", rendered="2026-10-18 01:29") + "\n<script>alert(1)</script> & co\n", encoding="utf-8")
    (d / "latest.md").write_text("ignored", encoding="utf-8")
    (d / "trends-2026-10-18.md").write_text("ignored", encoding="utf-8")
    (d / "2026-10-18.json").write_text("{}", encoding="utf-8")
    return d


@pytest.fixture
def gh_env(monkeypatch, digests, tmp_path):
    """Pin everything resolve_links reads, so the tests mean the same on a laptop and inside Actions."""
    monkeypatch.setenv("GH_REPO", "Owner/radar")
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.delenv("LITRADAR_PAGES_URL", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setenv("LITRADAR_OUT_DIR", str(digests))
    monkeypatch.setenv("LITRADAR_DB", str(tmp_path / "unused.sqlite"))
    monkeypatch.delenv("LITRADAR_CONFIG_DIR", raising=False)
    return digests


class FakeGh:
    """Stands in for subprocess.run: records every gh call, answers `issue list` and `issue create`, fails on request."""

    def __init__(self, issues=None, fail=None, next_number=42):
        self.issues = issues or []
        self.fail = fail or {}
        self.next_number = next_number
        self.calls = []

    def __call__(self, cmd, input=None, capture_output=True, text=True, timeout=None, check=False):
        assert cmd[0] == "gh" and capture_output and text and not check
        key = " ".join(cmd[1:3])
        self.calls.append({"args": list(cmd[1:]), "input": input})
        if key in self.fail:
            return subprocess.CompletedProcess(cmd, 1, "", self.fail[key])
        if key == "issue list":
            return subprocess.CompletedProcess(cmd, 0, json.dumps(self.issues), "")
        if key == "issue create":
            return subprocess.CompletedProcess(cmd, 0, f"https://github.com/Owner/radar/issues/{self.next_number}\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def of(self, *prefix):
        return [c for c in self.calls if c["args"][: len(prefix)] == list(prefix)]


def _issue(number, date, state="OPEN"):
    return {"number": number, "title": f"Weekly digest {date}", "state": state, "url": f"https://github.com/Owner/radar/issues/{number}"}


def _dsettings(digests, **delivery):
    return Settings(out_dir=digests, radar={"digest": {"title": "Science literature radar", "delivery": delivery}})


# ---- digest files and config -------------------------------------------------------------------
def test_list_digests_newest_first_and_only_dated_files(digests):
    assert [p.name for p in deliver.list_digests(digests)] == ["2026-10-18.md", "2026-10-11.md", "2026-10-04.md"]
    assert deliver.latest_digest(digests).name == "2026-10-18.md"
    assert deliver.latest_digest(digests, "2026-10-11").name == "2026-10-11.md"
    assert deliver.latest_digest(digests, "2026-01-01") is None
    assert deliver.list_digests(digests / "missing") == []


def test_delivery_config_defaults_and_coercion():
    cfg = deliver.delivery_config({})
    assert cfg == {"github_issue": True, "close_previous": True, "label": "digest", "feed_entries": 12, "pages_url": ""}
    cfg = deliver.delivery_config({"digest": {"delivery": {"github_issue": "false", "close_previous": 0, "label": " news ", "feed_entries": "5", "pages_url": "https://radar.example/"}}})
    assert cfg == {"github_issue": False, "close_previous": False, "label": "news", "feed_entries": 5, "pages_url": "https://radar.example/"}
    assert deliver.delivery_config({"digest": {"delivery": {"feed_entries": "lots"}}})["feed_entries"] == 12
    assert deliver.delivery_config({"digest": {"delivery": "nonsense"}})["github_issue"] is True


def test_repo_links_defaults_and_overrides():
    assert LINKS.pages_url == "https://owner.github.io/radar/"
    assert LINKS.file_url("2026-10-18") == "https://github.com/Owner/radar/blob/main/digests/2026-10-18.md"
    assert LINKS.feed_url() == "https://owner.github.io/radar/feed.xml"
    custom = deliver.RepoLinks(repo="o/r", branch="dev", server_url="https://ghe.example/", pages_url="https://radar.example")
    assert custom.file_url("2026-10-18") == "https://ghe.example/o/r/blob/dev/digests/2026-10-18.md"
    assert custom.feed_url() == "https://radar.example/feed.xml"


def test_resolve_repo_prefers_flag_then_env_and_validates(monkeypatch):
    monkeypatch.setenv("GH_REPO", "env/repo")
    assert deliver.resolve_repo("flag/repo") == "flag/repo"
    assert deliver.resolve_repo() == "env/repo"
    with pytest.raises(deliver.GhError):
        deliver.resolve_repo("not a repo")


# ---- issue body ----------------------------------------------------------------------------------
def test_issue_body_small_digest_is_complete_with_links():
    md = _digest("2026-10-18", lines=5)
    body, truncated = deliver.build_issue_body(md, "2026-10-18", LINKS)
    assert truncated is False and len(body) <= deliver.ISSUE_BODY_LIMIT
    assert LINKS.file_url("2026-10-18") in body and LINKS.pages_url in body and LINKS.feed_url() in body
    assert md.rstrip() in body and "truncated" not in body
    assert "Custom -> Issues" in body
    assert deliver.issue_title("2026-10-18") == "Weekly digest 2026-10-18"


def test_issue_body_truncates_at_a_line_boundary_within_the_limit():
    lines = [f"line {i:05d} " + "x" * 40 for i in range(3000)]  # ~153k characters
    md = "# Title\n\n" + "\n".join(lines) + "\n"
    body, truncated = deliver.build_issue_body(md, "2026-10-18", LINKS)
    assert truncated is True and len(body) <= deliver.ISSUE_BODY_LIMIT
    assert "truncated at 60,000 characters" in body and LINKS.file_url("2026-10-18") in body
    kept = body.split("\n\n_… truncated", 1)[0].splitlines()[2:]  # after the link line and the blank
    kept = [k for k in kept if k.startswith("line ")]
    assert len(kept) > 1000 and all(re.fullmatch(r"line \d{5} x{40}", k) for k in kept)  # no half line


def test_issue_body_never_exceeds_a_tiny_limit():
    md = "x" * 5000  # no line breaks at all
    for limit in (900, 1200, 2000):
        body, truncated = deliver.build_issue_body(md, "2026-10-18", LINKS, limit=limit)
        assert truncated is True and len(body) <= limit


def test_digest_title_and_rendered_timestamp():
    md = _digest("2026-10-18", rendered="2026-10-18 01:29")
    assert deliver.digest_title(md, "2026-10-18") == "Science literature radar: 2026-01-01 to 2026-10-18"
    assert deliver.digest_rendered_at(md, "2026-10-18") == "2026-10-18T01:29:00Z"
    assert deliver.digest_title("no heading here", "2026-10-18") == "Weekly digest 2026-10-18"
    assert deliver.digest_rendered_at("", "2026-10-18") == "2026-10-18T00:00:00Z"


# ---- gh subprocess: create vs update, close previous, toggles, failures -------------------------
def test_issue_is_created_when_none_exists(gh_env, monkeypatch):
    fake = FakeGh()
    monkeypatch.setattr(deliver, "_run", fake)
    res = deliver.deliver_github_issue(_dsettings(gh_env))
    assert res["ok"] and res["action"] == "created" and res["number"] == 42 and res["date"] == "2026-10-18"
    assert res["url"] == "https://github.com/Owner/radar/issues/42" and res["label"] == "digest" and res["closed"] == []
    listed = fake.of("issue", "list")
    assert len(listed) == 1 and "--state" in listed[0]["args"] and "all" in listed[0]["args"]
    created = fake.of("issue", "create")
    assert len(created) == 1
    args = created[0]["args"]
    assert args[args.index("--title") + 1] == "Weekly digest 2026-10-18" and args[-2:] == ["--body-file", "-"]
    assert created[0]["input"].startswith("_Full digest: [digests/2026-10-18.md](https://github.com/Owner/radar/blob/main/digests/2026-10-18.md)")
    assert "<script>alert(1)</script> & co" in created[0]["input"]  # the digest text is passed through, untouched, on stdin
    assert fake.of("label", "create")[0]["args"][2] == "digest"
    assert fake.of("issue", "edit")[0]["args"][:3] == ["issue", "edit", "42"] and "--add-label" in fake.of("issue", "edit")[0]["args"]
    assert fake.of("issue", "close") == [] and not fake.of("issue", "edit")[0]["input"]


def test_existing_issue_is_updated_and_only_older_open_ones_are_closed(gh_env, monkeypatch):
    fake = FakeGh(issues=[
        _issue(9, "2026-10-25"),                 # newer: must never be touched
        _issue(7, "2026-10-18"),                 # same date: updated in place
        _issue(5, "2026-10-11"),                 # older and open: closed with a comment
        _issue(3, "2026-10-04", state="CLOSED"),  # already closed: left alone
        {"number": 2, "title": "bug: the thing", "state": "OPEN", "url": "https://github.com/Owner/radar/issues/2"},
    ])
    monkeypatch.setattr(deliver, "_run", fake)
    res = deliver.deliver_github_issue(_dsettings(gh_env))
    assert res["ok"] and res["action"] == "updated" and res["number"] == 7 and res["closed"] == [5]
    assert fake.of("issue", "create") == [] and fake.of("label", "create") == []
    edits = fake.of("issue", "edit")
    assert edits[0]["args"][:3] == ["issue", "edit", "7"] and edits[0]["args"][-2:] == ["--body-file", "-"]
    assert edits[0]["input"] and "Weekly digest 2026-10-18" in edits[0]["args"]
    closes = fake.of("issue", "close")
    assert len(closes) == 1 and closes[0]["args"][:3] == ["issue", "close", "5"]
    comment = closes[0]["args"][closes[0]["args"].index("--comment") + 1]
    assert "#7" in comment and "Weekly digest 2026-10-18" in comment and "https://github.com/Owner/radar/issues/7" in comment


def test_close_previous_false_keeps_older_issues_open(gh_env, monkeypatch):
    fake = FakeGh(issues=[_issue(5, "2026-10-11")])
    monkeypatch.setattr(deliver, "_run", fake)
    res = deliver.deliver_github_issue(_dsettings(gh_env, close_previous=False))
    assert res["ok"] and res["action"] == "created" and res["closed"] == [] and fake.of("issue", "close") == []
    # the explicit argument wins over the config
    fake2 = FakeGh(issues=[_issue(5, "2026-10-11")])
    monkeypatch.setattr(deliver, "_run", fake2)
    assert deliver.deliver_github_issue(_dsettings(gh_env, close_previous=False), close_previous=True)["closed"] == [5]


def test_github_issue_disabled_in_config_skips_without_calling_gh(gh_env, monkeypatch):
    fake = FakeGh()
    monkeypatch.setattr(deliver, "_run", fake)
    res = deliver.deliver_github_issue(_dsettings(gh_env, github_issue=False))
    assert res["ok"] and res["action"] == "skipped" and "github_issue" in res["reason"] and fake.calls == []


def test_custom_label_and_pages_url_from_config(gh_env, monkeypatch):
    fake = FakeGh()
    monkeypatch.setattr(deliver, "_run", fake)
    res = deliver.deliver_github_issue(_dsettings(gh_env, label="radar", pages_url="https://radar.example"))
    assert res["ok"] and res["label"] == "radar" and res["pages_url"] == "https://radar.example/"
    assert fake.of("label", "create")[0]["args"][2] == "radar"
    assert "https://radar.example/feed.xml" in fake.of("issue", "create")[0]["input"]


def test_specific_date_and_missing_date(gh_env, monkeypatch):
    fake = FakeGh()
    monkeypatch.setattr(deliver, "_run", fake)
    res = deliver.deliver_github_issue(_dsettings(gh_env), date="2026-10-11")
    assert res["ok"] and res["title"] == "Weekly digest 2026-10-11"
    res = deliver.deliver_github_issue(_dsettings(gh_env), date="2026-01-01")
    assert res["ok"] is False and "no digest 2026-01-01.md" in res["error"]
    res = deliver.deliver_github_issue(Settings(out_dir=gh_env / "empty", radar={}))
    assert res["ok"] is False and "no dated digest" in res["error"]


def test_gh_missing_or_failing_is_reported_never_raised(gh_env, monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(deliver, "_run", missing)
    res = deliver.deliver_github_issue(_dsettings(gh_env))
    assert res["ok"] is False and "not installed" in res["error"]

    fake = FakeGh(fail={"issue create": "GraphQL: Issues are disabled for this repository (createIssue)"})
    monkeypatch.setattr(deliver, "_run", fake)
    res = deliver.deliver_github_issue(_dsettings(gh_env))
    assert res["ok"] is False and "Issues are disabled" in res["error"] and res["action"] is None

    fake = FakeGh(fail={"issue list": "HTTP 401: Bad credentials"})
    monkeypatch.setattr(deliver, "_run", fake)
    res = deliver.deliver_github_issue(_dsettings(gh_env))
    assert res["ok"] is False and "could not list issues" in res["error"] and fake.of("issue", "create") == []

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(a[0], 60)

    monkeypatch.setattr(deliver, "_run", slow)
    assert "timed out" in deliver.deliver_github_issue(_dsettings(gh_env))["error"]


def test_label_and_close_failures_do_not_fail_the_delivery(gh_env, monkeypatch, caplog):
    fake = FakeGh(issues=[_issue(5, "2026-10-11")], fail={"label create": "already exists", "issue edit": "label not found", "issue close": "HTTP 403"})
    monkeypatch.setattr(deliver, "_run", fake)
    res = deliver.deliver_github_issue(_dsettings(gh_env))
    assert res["ok"] and res["action"] == "created" and res["number"] == 42
    assert res["label"] is None and res["closed"] == [] and res["close_errors"][0]["number"] == 5
    assert "not applied" in caplog.text and "could not close #5" in caplog.text


def test_dry_run_only_reads(gh_env, monkeypatch):
    fake = FakeGh(issues=[_issue(5, "2026-10-11")])
    monkeypatch.setattr(deliver, "_run", fake)
    res = deliver.deliver_github_issue(_dsettings(gh_env), dry_run=True)
    assert res["ok"] and res["action"] == "would create" and res["would_close"] == [5] and res["body"].startswith("_Full digest:")
    assert [c["args"][:2] for c in fake.calls] == [["issue", "list"]]
    fake = FakeGh(issues=[_issue(7, "2026-10-18")])
    monkeypatch.setattr(deliver, "_run", fake)
    res = deliver.deliver_github_issue(_dsettings(gh_env), dry_run=True)
    assert res["action"] == "would update" and res["number"] == 7
    # a dry run still produces its plan when gh cannot list (no token locally): it assumes nothing exists
    fake = FakeGh(fail={"issue list": "not logged in"})
    monkeypatch.setattr(deliver, "_run", fake)
    res = deliver.deliver_github_issue(_dsettings(gh_env), dry_run=True)
    assert res["ok"] and res["action"] == "would create" and "not logged in" in res["lookup_error"]


# ---- feed ----------------------------------------------------------------------------------------
def test_feed_is_valid_atom_with_rfc3339_dates_escaped_content_and_links(digests):
    xml = deliver.build_feed(digests, LINKS, "Science literature radar", entries=12)
    root = ET.fromstring(xml)  # raises on malformed XML
    assert root.tag == f"{ATOM}feed" and root.find(f"{ATOM}title").text == "Science literature radar"
    assert RFC3339.match(root.find(f"{ATOM}updated").text) and root.find(f"{ATOM}updated").text == "2026-10-18T01:29:00Z"
    self_link = [ln for ln in root.findall(f"{ATOM}link") if ln.get("rel") == "self"][0]
    assert self_link.get("href") == "https://owner.github.io/radar/feed.xml"
    entries = root.findall(f"{ATOM}entry")
    assert [e.find(f"{ATOM}title").text for e in entries] == [
        "Science literature radar: 2026-01-01 to 2026-10-18",
        "Science literature radar: 2026-01-01 to 2026-10-11",
        "Science literature radar: 2026-01-01 to 2026-10-04",
    ]
    for e in entries:
        assert RFC3339.match(e.find(f"{ATOM}published").text) and RFC3339.match(e.find(f"{ATOM}updated").text)
        assert e.find(f"{ATOM}content").get("type") == "text" and e.find(f"{ATOM}id").text.startswith("https://github.com/Owner/radar/blob/main/digests/")
    assert entries[0].find(f"{ATOM}updated").text == "2026-10-18T01:29:00Z" and entries[2].find(f"{ATOM}updated").text == "2026-10-04T00:00:00Z"
    hrefs = {ln.get("rel"): ln.get("href") for ln in entries[0].findall(f"{ATOM}link")}
    assert hrefs == {"alternate": LINKS.file_url("2026-10-18"), "related": "https://owner.github.io/radar/"}
    raw = (digests / "2026-10-18.md").read_text(encoding="utf-8")
    content = entries[0].find(f"{ATOM}content").text
    assert content.startswith("# Science literature radar") and content.endswith("…") and content[:-1] == raw[:2000]
    assert "&lt;b&gt;" in xml and "<b>" not in xml and "&amp; more" in xml  # escaped on the wire, intact after parsing
    assert "<b>0</b> & more" in content
    assert len(ET.fromstring(deliver.build_feed(digests, LINKS, "T", entries=2)).findall(f"{ATOM}entry")) == 2


def test_feed_with_no_digests_is_still_valid(tmp_path):
    root = ET.fromstring(deliver.build_feed(tmp_path, LINKS, "T"))
    assert root.findall(f"{ATOM}entry") == [] and RFC3339.match(root.find(f"{ATOM}updated").text)


def test_feed_strips_characters_xml_forbids(tmp_path):
    (tmp_path / "2026-10-18.md").write_text("# T\x00itle\x07\n\nbody\x1f text\n", encoding="utf-8")
    xml = deliver.build_feed(tmp_path, LINKS, "T")
    e = ET.fromstring(xml).find(f"{ATOM}entry")
    assert e.find(f"{ATOM}title").text == "Title" and e.find(f"{ATOM}content").text == "# Title\n\nbody text\n"


def test_write_feed_creates_parent_dirs_and_reports(gh_env, tmp_path):
    out = tmp_path / "site" / "feed.xml"
    res = deliver.write_feed(_dsettings(gh_env, feed_entries=2), out)
    assert res["ok"] and res["entries"] == 2 and out.is_file() and res["url"] == "https://owner.github.io/radar/feed.xml"
    assert len(ET.parse(out).getroot().findall(f"{ATOM}entry")) == 2
    assert deliver.write_feed(_dsettings(gh_env), out, entries=1)["entries"] == 1
    res = deliver.write_feed(_dsettings(gh_env), tmp_path / "digests" / "2026-10-18.md" / "feed.xml")  # parent is a file
    assert res["ok"] is False and "cannot write" in res["error"]


# ---- the command ---------------------------------------------------------------------------------
def test_deliver_cli_requires_an_action(gh_env, capsys):
    assert cli.main(["deliver"]) == 2
    assert "pass --issue, --feed PATH and/or --status" in capsys.readouterr().err


def test_deliver_cli_feed_writes_a_parseable_file(gh_env, tmp_path, capsys):
    out = tmp_path / "site" / "feed.xml"
    assert cli.main(["deliver", "--feed", str(out)]) == 0
    assert "feed written to" in capsys.readouterr().out and len(ET.parse(out).getroot().findall(f"{ATOM}entry")) == 3


def test_deliver_cli_status_names_channels_and_never_prints_a_value(gh_env, monkeypatch, capsys):
    monkeypatch.setattr(deliver, "_run", FakeGh())
    monkeypatch.setenv("GH_TOKEN", "s3cret-token-value")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example/s3cret-hook")
    for name in ("SMTP_HOST", "DIGEST_FROM", "DIGEST_TO"):
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["deliver", "--status"]) == 0
    out = capsys.readouterr().out
    assert "e-mail (SMTP)   : not configured (missing SMTP_HOST, DIGEST_FROM, DIGEST_TO)" in out
    assert "Slack webhook   : configured" in out
    assert "GitHub Issue    : enabled; gh" in out and "token: GH_TOKEN" in out and "repo: Owner/radar" in out
    assert "Atom feed       : always available" in out and "https://owner.github.io/radar/feed.xml" in out
    assert "latest digest:" in out and "2026-10-18.md" in out and "3 dated digest file(s)" in out
    assert "s3cret" not in out
    assert cli.main(["deliver", "--status", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["slack"]["configured"] is True and rep["github_issue"]["token"] == "GH_TOKEN" and rep["digests"] == 3
    assert "s3cret" not in json.dumps(rep)


def test_deliver_cli_issue_dry_run_prints_the_plan_and_writes_nothing(gh_env, monkeypatch, capsys):
    fake = FakeGh(issues=[_issue(5, "2026-10-11")])
    monkeypatch.setattr(deliver, "_run", fake)
    assert cli.main(["deliver", "--issue", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry run: would create issue 'Weekly digest 2026-10-18' in Owner/radar" in out
    assert "would close: #5" in out and "body, first lines" in out and "_Full digest:" in out
    assert [c["args"][:2] for c in fake.calls] == [["issue", "list"]]


def test_deliver_cli_issue_failure_exits_1_with_an_actions_warning(gh_env, monkeypatch, capsys):
    monkeypatch.setattr(deliver, "_run", FakeGh(fail={"issue create": "GraphQL: Issues are disabled for this repository"}))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert cli.main(["deliver", "--issue"]) == 1
    out = capsys.readouterr().out
    assert "::warning::GitHub Issue delivery failed: gh issue create exited 1: GraphQL: Issues are disabled" in out
    monkeypatch.delenv("GITHUB_ACTIONS")
    assert cli.main(["deliver", "--issue"]) == 1
    assert "deliver: warning: GitHub Issue delivery failed" in capsys.readouterr().out


def test_deliver_cli_issue_success_and_skip(gh_env, monkeypatch, capsys):
    fake = FakeGh(issues=[_issue(5, "2026-10-11")])
    monkeypatch.setattr(deliver, "_run", fake)
    assert cli.main(["deliver", "--issue", "--date", "2026-10-18"]) == 0
    out = capsys.readouterr().out
    assert "issue created #42: Weekly digest 2026-10-18" in out and "closed previous: #5" in out
    with pytest.raises(SystemExit) as exc:
        cli.main(["deliver", "--issue", "--date", "2026-13-45"])
    assert exc.value.code == 2 and "not a real calendar date" in capsys.readouterr().err
    assert cli.main(["--config-dir", str(_write_config(gh_env.parent, github_issue=False)), "deliver", "--issue"]) == 0
    assert "GitHub Issue skipped: digest.delivery.github_issue is false" in capsys.readouterr().out


def _write_config(root, **delivery):
    """A config directory whose radar.yaml carries the given digest.delivery toggles."""
    d = root / "config"
    d.mkdir(exist_ok=True)
    (d / "radar.yaml").write_text(yaml.safe_dump({"digest": {"title": "T", "delivery": delivery}}), encoding="utf-8")
    return d


# ---- workflow wiring -----------------------------------------------------------------------------
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def test_every_workflow_action_is_pinned_to_a_full_commit_sha():
    pin = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
    seen = {}
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for job in _workflow(path.name)["jobs"].values():
            for step in job.get("steps", []):
                uses = step.get("uses")
                if uses:
                    assert pin.match(uses), f"{path.name}: unpinned action {uses!r}"
                    action, sha = uses.split("@")
                    assert seen.setdefault(action, sha) == sha, f"{action} pinned to two different SHAs"


def test_weekly_publish_job_delivers_the_issue_after_the_commit_with_issues_write():
    wf = _workflow("weekly.yml")
    publish = wf["jobs"]["publish"]
    assert publish["permissions"] == {"contents": "write", "issues": "write"}
    assert wf["jobs"]["radar"]["permissions"] == {"contents": "read"}  # the job with the API keys is unchanged
    names = [s.get("name", "") for s in publish["steps"]]
    commit = next(s for s in publish["steps"] if s.get("id") == "commit")
    assert 'echo "written=true" >> "$GITHUB_OUTPUT"' in commit["run"] and "git push" in commit["run"]
    assert commit["run"].index("git push") < commit["run"].index("written=true")  # outputs only after a successful push
    deliver_step = next(s for s in publish["steps"] if "deliver --issue" in s.get("run", ""))
    assert names.index(deliver_step["name"]) > names.index(commit["name"])
    assert deliver_step["if"] == "steps.commit.outputs.written == 'true'" and deliver_step["continue-on-error"] is True
    assert deliver_step["env"]["DIGEST_DATE"] == "${{ steps.commit.outputs.date }}" and '--date "$DIGEST_DATE"' in deliver_step["run"]
    assert "${{" not in deliver_step["run"]  # inputs reach the shell through env: only


def test_pages_feed_step_sits_between_the_dashboard_build_and_the_upload():
    build = _workflow("pages.yml")["jobs"]["build"]
    steps = build["steps"]
    i_dash = next(i for i, s in enumerate(steps) if "litradar dashboard" in s.get("run", ""))
    i_feed = next(i for i, s in enumerate(steps) if "deliver --feed site/feed.xml" in s.get("run", ""))
    i_upload = next(i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("actions/upload-pages-artifact@"))
    assert i_dash < i_feed < i_upload
    assert steps[i_feed]["if"] == "steps.db.outputs.found == 'true'" and steps[i_feed]["continue-on-error"] is True
    assert steps[i_upload]["with"]["path"] == "site"
