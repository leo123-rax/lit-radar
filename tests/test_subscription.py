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
        if isinstance(self._html, dict):
            self._html = next((v for k, v in self._html.items() if k in url), "<html></html>")
    def wait_for_load_state(self, *a, **k):
        pass
    def content(self):
        return self._html if isinstance(self._html, str) else "<html></html>"
    def evaluate(self, script):
        return "Mozilla/5.0 (Test) Chrome/1.0" if "userAgent" in script else None
    def close(self):
        pages = getattr(self.ctx, "pages", None)
        if pages is not None and self in pages:
            pages.remove(self)  # Playwright drops closed pages from context.pages


class FakeContext:
    def __init__(self, html, redirect):
        self.html, self.redirect = html, redirect  # html: str, or {url-substring: html}; tests may swap it
        self.pages = []
        self.cookie_store = []
    def new_page(self):
        pg = FakePage(self, self.html, "")
        self.pages.append(pg)
        return pg
    def cookies(self, url=None):
        return list(self.cookie_store)
    def add_cookies(self, cookies):
        self.cookie_store.extend(cookies)
    def storage_state(self, path=None):
        return {"cookies": list(self.cookie_store), "origins": []}
    def close(self):
        pass


class FakePW:
    def __init__(self, html, redirect):
        self.chromium = self
        self.html, self.redirect = html, redirect
    def launch_persistent_context(self, user_data_dir, headless=True, **kwargs):
        self.launch_kwargs = dict(kwargs, headless=headless)
        return FakeContext(self.html, self.redirect)
    def stop(self):
        pass


def test_login_detection_and_status(tmp_path):
    settings = Settings(cache_dir=tmp_path)
    # not signed in: the account page bounces to the identity provider
    pw_out = lambda: FakePW(PAYWALL_HTML, {})  # noqa: E731
    st = S.status(settings, factory=pw_out)
    assert st == {"nature": False, "science": False}
    # signed in: stays on nature.com, and Science shows a sign-out link
    pw_in = lambda: FakePW({"nature.com": NATURE_HTML, "science.org": SCIENCE_HTML}, {})  # noqa: E731
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
    assert not r.error and {"NovaSeq X Plus", "Chromium"} <= set(r.instruments) and "Illumina" in r.companies
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


def test_login_checks_in_the_live_window_after_cookies_change_and_saves_the_session(tmp_path):
    settings = Settings(cache_dir=tmp_path)
    probes, contexts = [], []
    class CountingPW(FakePW):
        def launch_persistent_context(self, user_data_dir, headless=True, **kwargs):
            ctx = super().launch_persistent_context(user_data_dir, headless, **kwargs)
            contexts.append((headless, ctx))
            orig = ctx.new_page
            def new_page():
                pg = orig()
                orig_goto = pg.goto
                def goto(url, **kw):
                    if S.PUBLISHERS["nature"]["probe_url"] in url:
                        probes.append(headless)
                    return orig_goto(url, **kw)
                pg.goto = goto
                return pg
            ctx.new_page = new_page
            return ctx
    ticks = [0.0]
    def clock():
        ticks[0] += 5.0
        return ticks[0]
    # A. nothing happens: no probe while the window is up; timeout -> one final headless probe (still paywalled)
    rep = S.login(settings, "nature", wait_s=60, poll_s=1, factory=lambda: CountingPW(PAYWALL_HTML, {}), sleep=lambda s: None, clock=clock, stdin_ready=lambda: False)
    assert rep["logged_in"] is False and rep["ended_by"] == "timeout" and probes == [False, True]
    # B. the user signs in: a cookie appears and the page turns into the signed-in article -> probed in the SAME (visible)
    #    context after the settle time, state saved, no headless probe needed
    probes.clear()
    contexts.clear()
    ticks[0] = 0.0
    def user_signs_in(_seconds):
        for headless, ctx in contexts:
            if not headless and not ctx.cookie_store:
                ctx.cookie_store.append({"name": "idp_session", "value": "x", "domain": ".nature.com", "path": "/"})
                ctx.html = {"nature.com": NATURE_HTML, "idp.nature.com": PAYWALL_HTML}
    rep = S.login(settings, "nature", wait_s=200, poll_s=1, factory=lambda: CountingPW(PAYWALL_HTML, {}), sleep=user_signs_in, clock=clock, stdin_ready=lambda: False, settle_s=10, probe_every_s=30)
    assert rep["logged_in"] is True and rep["ended_by"] == "signed in" and probes == [False, False]
    state = S.state_path(settings, "nature")
    assert state.exists() and [c["name"] for c in __import__("json").loads(state.read_text())["cookies"]] == ["idp_session"]
    # C. a later session (headless read) gets the saved cookies back and the window's user agent
    assert __import__("json").loads(state.read_text())["user_agent"] == "Mozilla/5.0 (Test) Chrome/1.0"
    pw_later = CountingPW(PAYWALL_HTML, {})
    with S.Session(settings, headless=True, factory=lambda: pw_later) as s2:
        assert [c["name"] for c in s2._context.cookie_store] == ["idp_session"]
    assert pw_later.launch_kwargs["user_agent"] == "Mozilla/5.0 (Test) Chrome/1.0" and pw_later.launch_kwargs["headless"] is True
    # D. Enter forces a check right away
    probes.clear()
    contexts.clear()
    ticks[0] = 0.0
    rep = S.login(settings, "nature", wait_s=60, poll_s=1, factory=lambda: CountingPW({"nature.com": PAYWALL_HTML}, {}), sleep=lambda s: None, clock=clock, stdin_ready=lambda: True)
    assert rep["ended_by"] in ("timeout", "window closed") and probes[0] is False and len(probes) >= 2
    assert S._left_sign_in(["https://www.nature.com/"], S.PUBLISHERS["nature"]) is True


SCIENCE_NESTED_HTML = """<html><body><h1 class="article-title">Nested page</h1>
<section id="bodymatter"><h2>Main</h2>
  <section><h2>Materials and Methods</h2><p>Cells were lysed and sequenced on a NovaSeq 6000 with TruSeq kits.</p></section>
  <section id="references"><h2>References and Notes</h2><p>1 T. Takeda, Autonomous binder design with Claude, Anthropic technical report (2026). Google Scholar</p></section>
</section></body></html>"""

NATURE_PREVIEW_HTML = """<html><body><h1 class="c-article-title">Preview paper</h1>
<section data-title="Abstract"><div class="c-article-section__content">This is an unedited manuscript that has been accepted for publication.</div></section>
<p>Accelerated Article Preview</p></body></html>"""

NATURE_SUPPLEMENT_HTML = """<html><body><h1 class="c-article-title">Supplement paper</h1>
<section data-title="Abstract"><div class="c-article-section__content">Abstract text.</div></section>
<section data-title="Methods"><div class="c-article-section__content">For completeness, the methods used in this study are described in the Supplementary Information.</div></section></body></html>"""


def test_reference_lists_and_nested_wrappers_stay_out_of_the_methods_text():
    art = S.extract_article(SCIENCE_NESTED_HTML, "science")
    methods = art["sections"]["methods"]
    assert "NovaSeq 6000" in methods and "Takeda" not in methods and "Anthropic" not in methods and "Google Scholar" not in methods


def test_preview_and_supplement_only_pages_are_labelled(tmp_path, monkeypatch):
    settings = Settings.from_env()
    settings.db_path = tmp_path / "p.sqlite"
    settings.cache_dir = tmp_path
    monkeypatch.setattr("litradar.ingest.Resolver.resolve", lambda self, ident: None, raising=False)
    art = S.extract_article(NATURE_PREVIEW_HTML, "nature")
    assert art["preview"] is True and not art["sections"].get("methods")
    from litradar.pipeline import Pipeline
    pipe = Pipeline(settings, http=object())
    r = S.read_one(pipe, settings, "10.1038/s41586-026-00001-1", html_override=NATURE_PREVIEW_HTML, store=False)
    assert r.preview is True and r.error.startswith("accelerated article preview")
    art2 = S.extract_article(NATURE_SUPPLEMENT_HTML, "nature")
    assert art2["methods_in_supplement"] is True and 0 < len(art2["sections"]["methods"]) < 800
    r2 = S.read_one(pipe, settings, "10.1038/s41586-026-00002-2", html_override=NATURE_SUPPLEMENT_HTML, store=False)
    assert r2.methods_in_supplement is True and not r2.error
