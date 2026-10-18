"""timeseries.html, built from a seeded `timeseries` cache (no network, no live database).

Cases: series per vendor and model aligned to the month axis (null = never fetched), the incomplete-months
flag, names and vendors from configuration with the store's mentions table as fallback, escaping of every
configured name, the empty-cache message, the committed CSV copied under its repository path and linked,
the NAV entry on every page, and the overview's monthly SUM staying single-counted after the cache is
consolidated into entity rows.
"""
import json
from datetime import date

import yaml

from litradar import dashboard
from litradar import timeseries as ts
from litradar.models import Mention, Paper
from litradar.store import Store

TODAY = date(2026, 10, 18)


def _config(tmp_path, evil=False):
    """A configuration directory with two vendors and three models; `evil` puts markup into every name."""
    cfg = tmp_path / "config"
    cfg.mkdir(exist_ok=True)
    name_a = '<script>alert("a")</script> Alpha' if evil else "Alpha 1000"
    vendor_a = 'Vendor <b>& Co</b>' if evil else "Vendor One"
    (cfg / "instruments.yaml").write_text(yaml.safe_dump({"entries": [
        {"id": "v1-alpha", "name": name_a, "vendor": vendor_a, "ticker": "VONE"},
        {"id": "v1-beta", "name": "Beta 2000", "vendor": vendor_a, "ticker": "VONE"},
        {"id": "v2-gamma", "name": "Gamma", "vendor": "Vendor Two", "ticker": "VTWO"},
    ]}, allow_unicode=True), encoding="utf-8")
    (cfg / "archive_models.yaml").write_text(yaml.safe_dump({"entities": {
        "v1-alpha": {"vendor": vendor_a, "sra": ["alpha 1000"], "ena": ["Alpha 1000"], "epmc_terms": ["Alpha 1000"]},
        "v1-beta": {"vendor": vendor_a, "sra": ["beta 2000", "beta 2000 plus"], "ena": ["Beta 2000", "Beta 2000 Plus"], "epmc_terms": ["Beta 2000"]},
        "v2-gamma": {"vendor": "Vendor Two", "sra": ["gamma"], "ena": ["Gamma"], "epmc_terms": ["Gamma"]},
    }}, allow_unicode=True), encoding="utf-8")
    return cfg


def _seed(tmp_path, name="radar.sqlite"):
    st = Store(tmp_path / name)
    months = ["2026-07", "2026-08", "2026-09", "2026-10"]
    for i, m in enumerate(months):
        st.put_timeseries_count("sra", "v1-alpha", "alpha 1000", m, 100 + i)
        st.put_timeseries_count("ena", "v1-alpha", "Alpha 1000", m, 200 + i)
        st.put_timeseries_count("epmc", "v1-alpha", "Alpha 1000", m, 3 + i)
        st.put_timeseries_count("sra", "v2-gamma", "gamma", m, 10 * i)
    # the entity-level key for a two-string model, only from 2026-09 on (2026-07/08 never fetched -> null)
    st.put_timeseries_count("sra", "v1-beta", "beta 2000|beta 2000 plus", "2026-09", 50)
    st.put_timeseries_count("sra", "v1-beta", "beta 2000|beta 2000 plus", "2026-10", 60)
    # an entity that is in the store's mentions but in no configuration file: labelled from the mentions table
    st.put_timeseries_count("sra", "zz-legacy", "legacy", "2026-10", 7)
    st.upsert_paper(Paper(id="doi:10.1000/ts1", source="biorxiv", kind="preprint", title="A paper", doi="10.1000/ts1",
                          abstract="", journal="J", pub_date="2026-10-13", url="https://example.org/1"))
    st.replace_mentions("doi:10.1000/ts1", [
        Mention("doi:10.1000/ts1", "instrument", "zz-legacy", "Legacy Box", "Vendor Three", "", "sequencer", "methods", 1, "…"),
    ])
    st.commit()
    st.close()
    return tmp_path / name


def _build(tmp_path, db, cfg=None, timeseries_dir=None, out="site"):
    out_dir = tmp_path / out
    report = dashboard.build(db, out_dir, weeks=4, today=TODAY, config_dir=cfg, timeseries_dir=timeseries_dir)
    page = (out_dir / "timeseries.html").read_text(encoding="utf-8")
    data = json.loads((out_dir / "data" / "timeseries.json").read_text(encoding="utf-8"))
    return report, page, data, out_dir


def test_page_series_vendors_axis_and_incomplete_flag(tmp_path):
    cfg = _config(tmp_path)
    report, page, data, out = _build(tmp_path, _seed(tmp_path), cfg)
    assert data["available"] is True and data["months"] == ["2026-07", "2026-08", "2026-09", "2026-10"]
    assert data["incomplete"] == ["2026-09", "2026-10"]           # the trailing two months are shaded as incomplete
    assert [s["id"] for s in data["sources"]] == ["sra", "ena", "epmc"]
    assert data["sources"][0] == {"id": "sra", "label": "SRA experiments", "unit": "experiments", "note": ts.UNIT_NOTES["sra"]}
    # vendors in configuration order, models in configuration order, the unconfigured id last under its mentions vendor
    assert [(v["name"], v["slug"], [e["id"] for e in v["entities"]]) for v in data["vendors"]] == [
        ("Vendor One", "vendor-one", ["v1-alpha", "v1-beta"]),
        ("Vendor Two", "vendor-two", ["v2-gamma"]),
        ("Vendor Three", "vendor-three", ["zz-legacy"]),
    ]
    alpha, beta = data["vendors"][0]["entities"]
    assert alpha["name"] == "Alpha 1000" and alpha["series"] == {"sra": [100, 101, 102, 103], "ena": [200, 201, 202, 203], "epmc": [3, 4, 5, 6]}
    assert beta["name"] == "Beta 2000" and beta["series"] == {"sra": [None, None, 50, 60]}   # null = never fetched, not 0
    assert data["vendors"][2]["entities"][0]["name"] == "Legacy Box" and data["vendors"][2]["entities"][0]["series"] == {"sra": [None, None, None, 7]}
    assert data["entities"] == 4 and data["series"] == 6 and data["rows"] == 19 and data["fetched_at"]
    assert data["json"] == "data/timeseries.json" and data["csv"] == ""   # no committed CSV handed to this build
    assert report["counts"]["timeseries_series"] == 6 and report["counts"]["timeseries_vendors"] == 3
    # the page: one card per vendor with a canvas and a table slot, the controls, the shading note
    assert page.count('class="card ts-vendor"') == 3 and page.count("<canvas") == 3
    for slug in ("vendor-one", "vendor-two", "vendor-three"):
        assert f'id="ts-{slug}-chart"' in page and f'id="ts-{slug}-table"' in page
    assert 'name="ts-source" value="sra" checked' in page and 'value="epmc"' in page and 'name="ts-scale" value="log"' in page
    assert "incomplete (deposit lag)" in page and "Shaded months (2026-09, 2026-10)" in page
    assert "no committed CSV was found" in page and 'href="data/timeseries.json"' in page
    assert "litradar.renderTimeseries(litradar.pageData());" in page
    assert "2 models: Alpha 1000, Beta 2000" in page


def test_every_configured_name_is_escaped(tmp_path):
    cfg = _config(tmp_path, evil=True)
    _, page, data, _ = _build(tmp_path, _seed(tmp_path), cfg)
    assert "<script>alert" not in page and "<b>&" not in page
    assert "&lt;script&gt;alert(&quot;a&quot;)&lt;/script&gt; Alpha" in page and "Vendor &lt;b&gt;&amp; Co&lt;/b&gt;" in page
    blob = page.split('<script type="application/json" id="page-data">', 1)[1].split("</script>", 1)[0]
    assert "<" not in blob and ">" not in blob and json.loads(blob)["timeseries"]["vendors"][0]["name"] == "Vendor <b>& Co</b>"
    assert data["vendors"][0]["slug"] == "vendor-b-co-b" and f'id="ts-{data["vendors"][0]["slug"]}-chart"' in page
    # the vendor's slug never carries markup into an id attribute
    assert all(ch.isalnum() or ch == "-" for v in data["vendors"] for ch in v["slug"])


def test_empty_cache_says_what_to_run(tmp_path):
    st = Store(tmp_path / "empty.sqlite")
    st.commit()
    st.close()
    report, page, data, out = _build(tmp_path, tmp_path / "empty.sqlite", _config(tmp_path))
    assert data["available"] is False and data["vendors"] == [] and data["months"] == []
    assert "<canvas" not in page
    assert "Run <code>litradar timeseries --all</code> first" in page and "run `litradar timeseries --all` first" in page
    assert report["counts"]["timeseries_series"] == 0 and report["counts"]["timeseries_vendors"] == 0
    # a store from before the table existed behaves the same way
    plain = tmp_path / "plain.sqlite"
    import sqlite3

    sqlite3.connect(plain).execute("CREATE TABLE papers (id TEXT)").connection.commit()
    report, page, data, _ = _build(tmp_path, plain, None, out="site-plain")
    assert data["available"] is False and "litradar timeseries --all" in page


def test_committed_csv_is_copied_under_its_repository_path_and_linked(tmp_path):
    src = tmp_path / "digests" / "timeseries"
    src.mkdir(parents=True)
    (src / "timeseries_2024-01_2026-09_pacb.csv").write_text("source,entity_id,model,month,count,unit\n", encoding="utf-8")
    (src / "timeseries_2023-01_2026-10_all.csv").write_text("source,entity_id,model,month,count,unit\nsra,v1-alpha,alpha 1000,2026-10,103,experiments\n", encoding="utf-8")
    (src / "timeseries_2025-01_2026-10_all.csv").write_text("x\n", encoding="utf-8")   # same --to, narrower range: loses
    (src / "notes.csv").write_text("not a series file\n", encoding="utf-8")
    report, page, data, out = _build(tmp_path, _seed(tmp_path), _config(tmp_path), timeseries_dir=src)
    rel = "digests/timeseries/timeseries_2023-01_2026-10_all.csv"
    assert data["csv"] == rel and (out / rel).read_text(encoding="utf-8").startswith("source,entity_id") and "103" in (out / rel).read_text(encoding="utf-8")
    assert f'<a href="{rel}" download>' in page and report["files"][rel] == (out / rel).stat().st_size
    # the choice rule on its own: _all first, then the latest --to, then the widest range; junk ignored
    assert dashboard._timeseries_csv_name(["timeseries_2024-01_2026-09_pacb.csv", "timeseries_2024-01_2026-10_ont.csv"]) == "timeseries_2024-01_2026-10_ont.csv"
    assert dashboard._timeseries_csv_name(["timeseries_2023-01_2026-10_all.csv", "timeseries_2020-01_2026-10_all.csv"]) == "timeseries_2020-01_2026-10_all.csv"
    assert dashboard._timeseries_csv_name(["notes.csv", "timeseries_broken.csv"]) == ""
    # a missing or empty directory is simply no link
    _, page2, data2, _ = _build(tmp_path, _seed(tmp_path, "b.sqlite"), _config(tmp_path), timeseries_dir=tmp_path / "nowhere", out="site2")
    assert data2["csv"] == "" and "no committed CSV was found" in page2


def test_nav_entry_everywhere_and_overview_sum_is_single_counted_after_consolidation(tmp_path):
    cfg = _config(tmp_path)
    db = tmp_path / "cons.sqlite"
    st = Store(db)
    # the earlier per-string layout for the two-string model, plus one single-string model
    for m, a, b in (("2026-09", 30, 20), ("2026-10", 36, 24)):
        st.put_timeseries_count("sra", "v1-beta", "beta 2000", m, a)
        st.put_timeseries_count("sra", "v1-beta", "beta 2000 plus", m, b)
        st.put_timeseries_count("sra", "v1-alpha", "alpha 1000", m, 100)
    st.commit()
    specs = ts.load_archive_models(cfg / "archive_models.yaml")
    before = dashboard.build(db, tmp_path / "s1", weeks=4, today=TODAY, config_dir=cfg)
    overview_before = json.loads((tmp_path / "s1" / "data" / "overview.json").read_text(encoding="utf-8"))["timeseries"]["series"]["sra"]
    rep = ts.consolidate_cache(st, specs, ["v1-alpha", "v1-beta"])
    st.close()
    assert rep == {"derived": 2, "pruned": 4, "partial": 0, "virtual": {("sra", "v1-beta", "beta 2000|beta 2000 plus", "2026-09"): 50,
                                                                        ("sra", "v1-beta", "beta 2000|beta 2000 plus", "2026-10"): 60},
                   "entities": [{"entity_id": "v1-beta", "source": "sra", "derived": 2, "pruned": 4}]}
    after = dashboard.build(db, tmp_path / "s2", weeks=4, today=TODAY, config_dir=cfg)
    overview_after = json.loads((tmp_path / "s2" / "data" / "overview.json").read_text(encoding="utf-8"))["timeseries"]["series"]["sra"]
    ts_after = json.loads((tmp_path / "s2" / "data" / "timeseries.json").read_text(encoding="utf-8"))
    assert overview_before == overview_after == [150, 160]          # per-string and entity layouts give the same monthly sums
    assert ts_after["vendors"][0]["entities"][1]["series"]["sra"] == [50, 60]
    assert before["counts"]["timeseries_series"] == after["counts"]["timeseries_series"] == 2
    # the NAV carries the page on every top-level page and on the nested company pages
    for page in ("index.html", "instruments.html", "timeseries.html", "companies.html", "subgroups.html", "countries.html", "search.html"):
        text = (tmp_path / "s2" / page).read_text(encoding="utf-8")
        assert '<a href="timeseries.html"' in text, page
    assert '<a href="timeseries.html" aria-current="page">Time series</a>' in (tmp_path / "s2" / "timeseries.html").read_text(encoding="utf-8")
    assert '<a href="../timeseries.html">Time series</a>' in (tmp_path / "s2" / "companies" / "graph.html").read_text(encoding="utf-8")
    assert ("timeseries.html", "Time series") in dashboard.NAV
