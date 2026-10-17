import json

from litradar import subscription as S
from litradar.config import Settings

NATURE_HTML = """<html><body><h1 class="c-article-title">A test article</h1>
<section data-title="Abstract"><div class="c-article-section__content">We did things.</div></section>
<section data-title="Methods"><div class="c-article-section__content">Libraries were sequenced on a NovaSeq X Plus (Illumina). Cells were loaded on the Chromium X (10x Genomics).</div></section>
<section data-title="Funding"><div class="c-article-section__content">Funded by Example Foundation.</div></section>
<section data-title="Competing interests"><div class="c-article-section__content">A.B. is an employee of Illumina.</div></section>
</body></html>"""
PAYWALL_HTML = """<html><body><h1 class="c-article-title">Locked</h1><section data-title="Abstract"><div class="c-article-section__content">x</div></section>
<div class="c-article-access-provider">Access options</div></body></html>"""
SCIENCE_HTML = """<html><body><h1 class="article-title">Science paper</h1><section id="abstract"><h2>Abstract</h2><p>Short.</p></section>
<section id="sec-1"><h2>Materials and Methods</h2><p>Proteins were analysed on an Orbitrap Astral.</p></section>
<section id="sec-2"><h2>Acknowledgments</h2><p>We thank Twist Bioscience.</p></section></body></html>"""


def test_publisher_routing():
    assert S.publisher_for("10.1038/s41592-026-03265-w") == "nature"
    assert S.publisher_for("https://www.science.org/doi/10.1126/science.aeh2982") == "science"
    assert S.publisher_for("10.1016/j.cell.2026.01.001") is None
    pub, url, doi = S.article_url("10.1038/s41592-026-03265-w")
    assert (pub, url, doi) == ("nature", "https://www.nature.com/articles/s41592-026-03265-w", "10.1038/s41592-026-03265-w")


def test_extract_nature_and_science():
    a = S.extract_article(NATURE_HTML, "nature")
    assert a["title"] == "A test article" and a["abstract"] == "We did things." and not a["paywalled"]
    assert "NovaSeq X Plus" in a["sections"]["methods"] and "Example Foundation" in a["sections"]["funding"] and "Illumina" in a["sections"]["coi"]
    p = S.extract_article(PAYWALL_HTML, "nature")
    assert p["paywalled"] and "methods" not in p["sections"]
    s = S.extract_article(SCIENCE_HTML, "science")
    assert "Orbitrap Astral" in s["sections"]["methods"] and "Twist" in s["sections"]["ack"] and s["title"] == "Science paper"


def test_ledger_cap_and_pacing(tmp_path):
    t = [1000.0]
    led = S.ReadLedger(tmp_path / "l.json", cap=2, interval=20, clock=lambda: t[0])
    assert led.check()[0]
    led.record()
    led.record()
    ok, why = led.check()
    assert not ok and "cap" in why
    slept = []
    t[0] += 5
    led.wait_turn(sleep=lambda s: slept.append(s))
    assert slept and 14.9 < slept[0] < 15.1
    assert json.loads((tmp_path / "l.json").read_text())["last_ts"] == 1000.0


class FakePage:
    def __init__(self, ctx, html, url):
        self.ctx, self._html, self.url = ctx, html, url
    def goto(self, url, **kw):
        self.url = self.ctx.redirect.get(url, url)
    def wait_for_load_state(self, *a, **k):
        pass
    def content(self):
        return self._html
    def close(self):
        pass


class FakeContext:
    def __init__(self, html, redirect):
        self.html, self.redirect = html, redirect
    def new_page(self):
        return FakePage(self, self.html, "")
    def close(self):
        pass


class FakePW:
    def __init__(self, html, redirect):
        self.chromium = self
        self.html, self.redirect = html, redirect
    def launch_persistent_context(self, user_data_dir, headless=True):
        return FakeContext(self.html, self.redirect)
    def stop(self):
        pass


def test_login_detection_and_status(tmp_path):
    settings = Settings(cache_dir=tmp_path)
    # not signed in: the account page bounces to the identity provider
    pw_out = lambda: FakePW("<html>Sign in</html>", {"https://www.nature.com/my-account": "https://idp.nature.com/auth/personal/springernature"})  # noqa: E731
    st = S.status(settings, factory=pw_out)
    assert st == {"nature": False, "science": False}
    # signed in: stays on nature.com, and Science shows a sign-out link
    pw_in = lambda: FakePW("<html><a href='/action/showLogout'>Log out</a></html>", {})  # noqa: E731
    assert S.status(settings, factory=pw_in) == {"nature": True, "science": True}
    rep = S.login(settings, "nature", wait_s=10, poll_s=1, factory=pw_in, sleep=lambda s: None)
    assert rep["logged_in"] and rep["note"] == "already signed in"
    ticks = [0.0]
    def clock():
        ticks[0] += 3.0
        return ticks[0]
    rep = S.login(settings, "science", wait_s=10, poll_s=1, factory=pw_out, sleep=lambda s: None, clock=clock)
    assert not rep["logged_in"]


def test_read_one_facts_only_and_keep_text(tmp_path, monkeypatch):
    from litradar.pipeline import Pipeline

    settings = Settings.from_env()
    settings.db_path = tmp_path / "r.sqlite"
    settings.cache_dir = tmp_path
    pipe = Pipeline(settings, http=object())
    monkeypatch.setattr("litradar.ingest.Resolver.resolve", lambda self, ident: None, raising=False)
    r = S.read_one(pipe, settings, "10.1038/s41592-026-99999-9", html_override=NATURE_HTML)
    assert not r.error and {"NovaSeq X", "Chromium"} <= set(r.instruments) and "Illumina" in r.companies
    row = pipe.store.get_paper(r.paper_id)
    assert row["fulltext_available"] == 0 and json.loads(row["sections_json"]) == {} and r.stored_as == "facts_only"
    r2 = S.read_one(pipe, settings, "10.1038/s41592-026-99999-9", keep_text=True, html_override=NATURE_HTML)
    row = pipe.store.get_paper(r2.paper_id)
    assert "NovaSeq X Plus" in json.loads(row["sections_json"])["methods"] and json.loads(row["extra_json"])["fulltext_source"] == "personal_subscription"
    bad = S.read_one(pipe, settings, "10.1016/j.cell.2026.01.001", html_override="<html></html>")
    assert "not a configured subscription host" in bad.error
    locked = S.read_one(pipe, settings, "10.1038/s41592-026-00000-0", html_override=PAYWALL_HTML)
    assert "paywall markers present" in locked.error
    assert "Methods" in S.format_card(r)
