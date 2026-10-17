"""Semantic Scholar enrichment against canned payloads (no network). Http.post_json / Http.get are faked."""
import json

from litradar.models import Paper
from litradar.sources import semantic_scholar as s2
from litradar.store import Store


class FakeResponse:
    def __init__(self, status_code, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeHttp:
    """Records POST bodies; answers post_json from a queue and get from a second queue."""

    def __init__(self, post_payloads=(), get_responses=()):
        self.post_payloads = list(post_payloads)
        self.get_responses = list(get_responses)
        self.posts = []  # (url, json_body, headers)
        self.gets = []  # (url, params, headers)

    def post_json(self, url, json_body, headers=None):
        self.posts.append((url, json_body, headers or {}))
        return self.post_payloads.pop(0) if self.post_payloads else None

    def get(self, url, params=None, headers=None, cache=False, ok_statuses=(200,)):
        self.gets.append((url, params, headers or {}))
        return self.get_responses.pop(0) if self.get_responses else None


def _paper(i, doi=None, cited=0, pub_date="2026-10-12", extra=None):
    doi = doi or f"10.1000/s2test{i}"
    return Paper(
        id=f"doi:{doi}", source="crossref", kind="article", title=f"Paper {i}", doi=doi, abstract="abs",
        journal="Journal", pub_date=pub_date, url=f"https://doi.org/{doi}", cited_by_count=cited, extra=dict(extra or {}),
    )


def _item(doi, cited, influential=0, tldr="summary", pub="2026-01-02", paper_id="abc"):
    return {
        "paperId": paper_id, "externalIds": {"DOI": doi, "CorpusId": 1}, "title": "T", "citationCount": cited,
        "influentialCitationCount": influential, "publicationDate": pub, "tldr": {"model": "tldr@v2.0.0", "text": tldr} if tldr is not None else None,
        "authors": [{"authorId": "1", "name": "A"}],
    }


def test_normal_payload_updates_counts_and_extra(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    st.upsert_paper(_paper(1, cited=0, extra={"pmcid": "PMC1"}))  # gains citations
    st.upsert_paper(_paper(2, cited=50))  # store already higher than S2 -> MAX keeps 50
    st.upsert_paper(_paper(3, doi=None))  # unknown to S2 -> null
    st.commit()
    payload = [
        _item("10.1000/S2TEST1", 12, influential=3, tldr="One-line gist."),  # upper-case DOI echoes back normalised
        _item("10.1000/s2test2", 7, influential=1),
        None,
    ]
    http = FakeHttp(post_payloads=[payload])
    out = s2.enrich_citations(http, st, api_key="secret-key", retry_wait=0)

    assert out["candidates"] == 3 and out["requested"] == 3 and out["matched"] == 2 and out["unmatched"] == 1
    assert out["updated"] == 1 and out["batches"] == 1 and out["failed_batches"] == 0
    assert out["citations"] == {"doi:10.1000/s2test1": 12, "doi:10.1000/s2test2": 50}

    p1 = st.get_paper("doi:10.1000/s2test1")
    assert p1["cited_by_count"] == 12
    extra1 = json.loads(p1["extra_json"])
    assert extra1["pmcid"] == "PMC1"  # pre-existing extra preserved
    assert extra1["semantic_scholar"]["influential_citation_count"] == 3
    assert extra1["semantic_scholar"]["tldr"] == "One-line gist."
    assert extra1["semantic_scholar"]["publication_date"] == "2026-01-02"
    assert extra1["semantic_scholar"]["fetched_at"]
    p2 = st.get_paper("doi:10.1000/s2test2")
    assert p2["cited_by_count"] == 50  # MAX(existing, new)
    assert json.loads(p2["extra_json"])["semantic_scholar"]["citation_count"] == 7
    assert "semantic_scholar" not in json.loads(st.get_paper("doi:10.1000/s2test3")["extra_json"])

    # request shape: fields in the query string, DOI: prefixed ids in the body, key in x-api-key
    url, body, headers = http.posts[0]
    assert url == "https://api.semanticscholar.org/graph/v1/paper/batch?fields=" + s2.FIELDS
    assert body == {"ids": ["DOI:10.1000/s2test1", "DOI:10.1000/s2test2", "DOI:10.1000/s2test3"]}
    assert headers["x-api-key"] == "secret-key" and headers["Accept"] == "application/json"

    # in-memory refresh helper
    papers = [_paper(1), _paper(2, cited=50)]
    assert s2.apply_to_papers(papers, out["citations"]) == 1 and papers[0].cited_by_count == 12


def test_no_key_sends_no_header_and_since_filters_candidates(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    st.upsert_paper(_paper(1, pub_date="2026-10-12"))
    st.upsert_paper(_paper(2, pub_date="2026-09-01"))  # outside the window
    st.commit()
    http = FakeHttp(post_payloads=[[_item("10.1000/s2test1", 1)]])
    out = s2.enrich_citations(http, st, since="2026-10-10", retry_wait=0)
    assert out["candidates"] == 1 and out["matched"] == 1
    assert http.posts[0][1] == {"ids": ["DOI:10.1000/s2test1"]}
    assert "x-api-key" not in http.posts[0][2]


def test_empty_payload_and_no_candidates(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    # no papers with DOIs at all -> no HTTP call
    http = FakeHttp(post_payloads=[[]])
    out = s2.enrich_citations(http, st, retry_wait=0)
    assert out == {"candidates": 0, "requested": 0, "matched": 0, "updated": 0, "unmatched": 0, "batches": 0, "failed_batches": 0, "citations": {}}
    assert http.posts == []
    # empty list back from the service -> nothing matched, nothing failed, store untouched
    st.upsert_paper(_paper(1, cited=4))
    st.commit()
    out = s2.enrich_citations(http, st, retry_wait=0)
    assert out["requested"] == 1 and out["matched"] == 0 and out["unmatched"] == 1 and out["failed_batches"] == 0
    assert st.get_paper("doi:10.1000/s2test1")["cited_by_count"] == 4


def test_malformed_payloads_do_not_crash_or_corrupt(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    st.upsert_paper(_paper(1, cited=4))
    st.upsert_paper(_paper(2, cited=0))
    st.commit()
    # a dict (error body on 200) instead of the documented list -> failed batch, enrichment stops
    http = FakeHttp(post_payloads=[{"message": "Bad query parameters", "code": "400"}])
    out = s2.enrich_citations(http, st, retry_wait=0)
    assert out["failed_batches"] == 1 and out["matched"] == 0 and out["requested"] == 0
    assert st.get_paper("doi:10.1000/s2test1")["cited_by_count"] == 4
    # junk elements: a string, a dict without ids, a non-numeric count, tldr as a bare string, a missing externalIds
    junk = ["garbage", {"foo": "bar"}, {"externalIds": {"DOI": "10.1000/s2test1"}, "citationCount": "abc", "tldr": "plain text"}]
    http = FakeHttp(post_payloads=[junk])
    out = s2.enrich_citations(http, st, retry_wait=0)
    assert out["failed_batches"] == 0 and out["requested"] == 2
    # 3 elements for 2 ids is off-contract, so position means nothing and only the echoed DOI counts:
    # "garbage" and {"foo": "bar"} carry no DOI and are dropped; the third element names s2test1 -> 1 matched, 1 unmatched
    assert out["matched"] == 1 and out["unmatched"] == 1 and out["updated"] == 0
    p1 = json.loads(st.get_paper("doi:10.1000/s2test1")["extra_json"])["semantic_scholar"]
    assert p1["citation_count"] == 0 and p1["tldr"] == "plain text" and p1["paper_id"] == ""
    assert st.get_paper("doi:10.1000/s2test1")["cited_by_count"] == 4  # MAX keeps the stored value over a junk 0
    assert "semantic_scholar" not in json.loads(st.get_paper("doi:10.1000/s2test2")["extra_json"] or "{}")
    # on-contract length with a junk element in it: position 1 ({"foo": "bar"}) IS the 2nd requested DOI, zero counts
    http = FakeHttp(post_payloads=[[None, {"foo": "bar"}]])
    out = s2.enrich_citations(http, st, retry_wait=0)
    assert out["matched"] == 1 and out["unmatched"] == 1
    p2 = json.loads(st.get_paper("doi:10.1000/s2test2")["extra_json"])["semantic_scholar"]
    assert p2["citation_count"] == 0 and p2["tldr"] == "" and p2["paper_id"] == ""
    # unreadable existing extra_json is replaced rather than raising
    st.conn.execute("UPDATE papers SET extra_json='not json' WHERE id='doi:10.1000/s2test1'")
    st.commit()
    http = FakeHttp(post_payloads=[[_item("10.1000/s2test1", 9), None]])
    out = s2.enrich_citations(http, st, retry_wait=0)
    assert out["matched"] == 1
    assert json.loads(st.get_paper("doi:10.1000/s2test1")["extra_json"])["semantic_scholar"]["citation_count"] == 9
    assert st.get_paper("doi:10.1000/s2test1")["cited_by_count"] == 9


def test_batching_500_per_call_and_failure_handling(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    for i in range(1200):
        st.upsert_paper(_paper(i))
    st.commit()
    # Candidates are ordered pub_date DESC, first_seen DESC, id. The 1200 upserts above can straddle a wall-clock
    # second (first_seen has 1 s resolution), which would reshuffle batch membership between runs; pin it so the
    # test is deterministic and the order is by id alone.
    st.conn.execute("UPDATE papers SET first_seen='2026-10-12T00:00:00Z'")
    st.commit()
    # three batches: 500, 500, 200
    http = FakeHttp(post_payloads=[[], [], []])
    out = s2.enrich_citations(http, st, retry_wait=0)
    assert out["batches"] == 3 and [len(b["ids"]) for _, b, _ in http.posts] == [500, 500, 200]
    assert out["requested"] == 1200 and all(i.startswith("DOI:10.1000/") for _, b, _ in http.posts for i in b["ids"])
    assert out["unmatched"] == 1200 and out["failed_batches"] == 0  # an empty list is a (useless) answer, not a failed request
    first_doi = http.posts[0][1]["ids"][0][len("DOI:"):]  # whichever DOI the store orders first
    # batch_size above the documented limit is clamped; below it is honoured
    http = FakeHttp(post_payloads=[[]] * 12)
    out = s2.enrich_citations(http, st, batch_size=100, limit=250, retry_wait=0)
    assert out["candidates"] == 250 and [len(b["ids"]) for _, b, _ in http.posts] == [100, 100, 50]
    # None = HTTP error / 429 after the client's own handling. Each failed batch is retried `retries` times, then skipped;
    # two failed batches IN A ROW end the run: batch 1 (3 attempts) + batch 2 (3 attempts) -> stop, no 3rd batch
    http = FakeHttp(post_payloads=[None] * 6)
    out = s2.enrich_citations(http, st, retries=2, retry_wait=0)
    assert len(http.posts) == 6 and out["batches"] == 2 and out["failed_batches"] == 2 and out["requested"] == 0
    assert [len(b["ids"]) for _, b, _ in http.posts] == [500] * 6
    # a poison batch at the head is skipped and the older papers behind it still get enriched
    http = FakeHttp(post_payloads=[None, [], []])
    out = s2.enrich_citations(http, st, retries=0, retry_wait=0)
    assert len(http.posts) == 3 and out["batches"] == 3 and out["failed_batches"] == 1 and out["requested"] == 700
    # a failure on the second batch keeps the first batch's results and still runs the third
    http = FakeHttp(post_payloads=[[_item(first_doi, 3)] + [None] * 499, None, []])
    out = s2.enrich_citations(http, st, retries=0, retry_wait=0)
    assert len(http.posts) == 3 and out["batches"] == 3 and out["failed_batches"] == 1
    assert out["requested"] == 700 and out["matched"] == 1 and st.get_paper(f"doi:{first_doi}")["cited_by_count"] == 3
    # success resets the consecutive-failure counter: ok, fail, ok, fail, ok -> all five batches run
    http = FakeHttp(post_payloads=[[], None, [], None, []])
    out = s2.enrich_citations(http, st, batch_size=100, limit=500, retries=0, retry_wait=0)
    assert len(http.posts) == 5 and out["batches"] == 5 and out["failed_batches"] == 2 and out["requested"] == 300
    # ... but two in a row after a success stop the run: ok, fail, fail -> batches 4 and 5 never requested
    http = FakeHttp(post_payloads=[[], None, None, [], []])
    out = s2.enrich_citations(http, st, batch_size=100, limit=500, retries=0, retry_wait=0)
    assert len(http.posts) == 3 and out["batches"] == 3 and out["failed_batches"] == 2 and out["requested"] == 100


def test_positional_mapping_for_merged_preprint_and_journal_records(tmp_path):
    """Semantic Scholar merges a preprint and its journal version into one record and echoes only ONE of the
    two DOIs in externalIds. Both stored rows must receive the merged counts; neither may be 'unmatched'."""
    st = Store(tmp_path / "t.sqlite")
    preprint, journal = "10.1101/2023.04.30.538439", "10.1038/s41592-025-02617-2"
    st.upsert_paper(_paper(1, doi=preprint, pub_date="2023-05-01"))
    st.upsert_paper(_paper(2, doi=journal, pub_date="2025-02-14"))
    st.commit()
    # request order is newest first: journal, then preprint; both elements echo the journal DOI
    merged = _item(journal, 177, influential=10, pub="2023-02-14", paper_id="0f9c053f")
    http = FakeHttp(post_payloads=[[merged, dict(merged)]])
    out = s2.enrich_citations(http, st, retry_wait=0)
    assert http.posts[0][1]["ids"] == [f"DOI:{journal}", f"DOI:{preprint}"]
    assert out["matched"] == 2 and out["unmatched"] == 0 and out["updated"] == 2
    for doi in (journal, preprint):
        row = st.get_paper(f"doi:{doi}")
        assert row["cited_by_count"] == 177
        assert json.loads(row["extra_json"])["semantic_scholar"]["paper_id"] == "0f9c053f"
    # the reverse: the element at position 1 echoes a DOI from position 0 -> still keyed by position
    http = FakeHttp(post_payloads=[[None, _item(journal, 200)]])
    out = s2.enrich_citations(http, st, retry_wait=0)
    assert out["matched"] == 1 and st.get_paper(f"doi:{preprint}")["cited_by_count"] == 200
    assert st.get_paper(f"doi:{journal}")["cited_by_count"] == 177


def test_null_cited_by_count_and_updated_at_change_marker(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    st.upsert_paper(_paper(1, cited=0))
    st.upsert_paper(_paper(2, cited=9))
    st.commit()
    # a backfill/import path may leave the nullable column NULL; SQLite's MAX(NULL, n) is NULL, so COALESCE is required
    st.conn.execute("UPDATE papers SET cited_by_count=NULL, updated_at='2026-01-01T00:00:00Z'")
    st.commit()
    http = FakeHttp(post_payloads=[[_item("10.1000/s2test1", 5), _item("10.1000/s2test2", 0)]])
    out = s2.enrich_citations(http, st, retry_wait=0)
    assert out["matched"] == 2 and out["updated"] == 1 and out["citations"] == {"doi:10.1000/s2test1": 5, "doi:10.1000/s2test2": 0}
    p1, p2 = st.get_paper("doi:10.1000/s2test1"), st.get_paper("doi:10.1000/s2test2")
    assert p1["cited_by_count"] == 5 and p2["cited_by_count"] == 0  # neither NULL
    # updated_at moves only where the count rose; the fetch time is recorded in extra_json for every matched row
    assert p1["updated_at"] != "2026-01-01T00:00:00Z" and p2["updated_at"] == "2026-01-01T00:00:00Z"
    assert json.loads(p2["extra_json"])["semantic_scholar"]["fetched_at"] == json.loads(p1["extra_json"])["semantic_scholar"]["fetched_at"]
    # a second run with the same count: extra_json refreshed, updated_at untouched
    marker = p1["updated_at"]
    st.conn.execute("UPDATE papers SET updated_at='2026-02-02T00:00:00Z' WHERE id='doi:10.1000/s2test1'")
    st.commit()
    http = FakeHttp(post_payloads=[[_item("10.1000/s2test1", 5), _item("10.1000/s2test2", 0)]])
    out = s2.enrich_citations(http, st, retry_wait=0)
    assert out["updated"] == 0 and st.get_paper("doi:10.1000/s2test1")["updated_at"] == "2026-02-02T00:00:00Z"
    assert marker  # (first run did set it)
    # and a rise moves it again
    http = FakeHttp(post_payloads=[[_item("10.1000/s2test1", 6), None]])
    out = s2.enrich_citations(http, st, retry_wait=0)
    assert out["updated"] == 1 and st.get_paper("doi:10.1000/s2test1")["updated_at"] != "2026-02-02T00:00:00Z"


def test_paper_details_and_parsers():
    body = _item("10.1038/s41592-024-02201-0", 1462, influential=133, pub="2024-02-26", paper_id="13dc81fc")
    body.update({"title": "scGPT", "venue": "Nature Methods", "year": 2024})
    http = FakeHttp(get_responses=[FakeResponse(200, body), FakeResponse(404, {"error": "Paper not found"}), None, FakeResponse(200, None)])
    d = s2.paper_details(http, "https://doi.org/10.1038/S41592-024-02201-0", api_key="k")
    assert d["paper_id"] == "13dc81fc" and d["citation_count"] == 1462 and d["influential_citation_count"] == 133
    assert d["venue"] == "Nature Methods" and d["publication_date"] == "2024-02-26" and d["external_ids"]["CorpusId"] == 1
    url, params, headers = http.gets[0]
    assert url == "https://api.semanticscholar.org/graph/v1/paper/DOI:10.1038/s41592-024-02201-0"
    assert params == {"fields": s2.DETAIL_FIELDS} and headers["x-api-key"] == "k"
    assert s2.paper_details(http, "10.9999/does-not-exist") is None  # 404
    assert s2.paper_details(http, "10.9999/x") is None  # client gave up (None)
    assert s2.paper_details(http, "10.9999/y") is None  # 200 but not JSON
    assert s2.paper_details(http, "not a doi") is None and len(http.gets) == 4

    assert s2.parse_item(None) is None and s2.parse_item("x") is None
    assert s2.parse_item({"tldr": None, "publicationDate": None})["tldr"] == "" and s2.parse_item({})["publication_date"] == ""
    assert s2.parse_batch({"not": "a list"}, ["10.1/a"]) is None
    assert s2.parse_batch([], ["10.1/a"]) == {}
    assert s2.parse_batch([None], ["10.1/a"]) == {}
    assert list(s2.parse_batch([_item("10.1000/zzz", 1)], ["10.1000/a"])) == ["10.1000/a"]  # positional beats the echoed DOI
    # off-contract length (3 elements for 2 ids): only elements echoing a requested DOI are kept
    assert list(s2.parse_batch([_item("10.1000/b", 1), _item("10.1000/zzz", 2), None], ["10.1000/a", "10.1000/b"])) == ["10.1000/b"]
    assert list(s2._chunks([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]

    # look-back window for the wiring: a 7-day run window alone enriches almost nothing (fresh papers sit at 0)
    assert s2.window_start("2026-10-10") == "2025-10-10" and s2.DEFAULT_LOOKBACK_DAYS == 365
    assert s2.window_start("2026-10-10", 30) == "2026-09-10" and s2.window_start("2026-10-10T12:00:00Z", 1) == "2026-10-09"
    assert s2.window_start("2026-10-10", 0) == "2026-10-10" and s2.window_start(None) is None and s2.window_start("") is None
    assert s2.window_start("not-a-date", 30) == "not-a-date"
