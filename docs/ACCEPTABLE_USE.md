# Acceptable use

This tool is built so that it is lawful for a **commercial operator with no institutional licence**,
run from a laptop or GitHub Actions with the operator's own API keys. Using it means agreeing to
the following. The reasoning and the per-source terms are in `docs/LEGAL.md`.

1. **Bring your own keys; keep them out of the repository.** Keys live in a git-ignored `.env` or in
   GitHub Actions secrets, never in YAML, Markdown, digests or issues. Never configure a publisher
   subscription login anywhere: the pipeline does not log in, and a reading subscription grants no
   automated-download or text-mining rights.
2. **Metadata and abstracts are stored from every source; full text only where the record's own
   licence allows it.** Open-licensed text (CC0, CC BY family, the PMC/Europe PMC open-access
   subset) may be stored and indexed. All-rights-reserved preprints and anything behind a paywall are
   link-only: the digest shows the title and the DOI, and the human reads it in a browser.
3. **Use the designated machine routes only.** Documented APIs (bioRxiv API, Europe PMC REST,
   Crossref, OpenAlex, E-utilities, ENA, arXiv export API, openFDA, ClinicalTrials.gov, NIH RePORTER,
   PRIDE, CELLxGENE, GDELT DOC 2.0, SEC EDGAR Atom, SciELO ArticleMeta, the ChinaXiv and KCI OAI-PMH
   endpoints, and the J-STAGE WebAPI when opted in) and RSS feeds that serve scripts. The one
   exception is the CNCB-NGDC archive pair (the GSA search page and the GSA-Human browse list):
   server-rendered metadata pages of a public data archive that publishes no API, permitted by its
   `robots.txt`, read at one request per second without touching the pages that defend themselves;
   the reasoning is in `docs/LEGAL.md` section 3 and any further exception needs the same case made
   there. Do not add HTML scrapers for publisher sites, Google Scholar, or any site whose
   `robots.txt` or `/.well-known/tdmrep.json` reserves rights. Pull requests that do will be closed.
4. **Identify yourself and stay under the limits.** Set `LITRADAR_CONTACT_EMAIL`; keep the per-host
   pacing in `litradar/http.py` at or below each source's documented rate; back off on 429.
5. **Do not re-serve stored text.** The local database and HTTP cache are for the operator's own
   search. Redistribute metadata, counts and links only, with attribution where a licence asks for it
   (OpenAlex, Semantic Scholar ODC-BY, NLM, Europe PMC, GDELT, KCI; "Powered by J-STAGE" with a link
   if you opt in). Remember that a public repository makes the committed digests and the database
   snapshot public too.
6. **Google News RSS is an opt-in.** Its `robots.txt` disallows the RSS path to all agents and
   Google's terms forbid automated access that contravenes it. If you enable it, that is your call
   and your risk.
7. **Institutional licences are yours to honour.** If you hold a text-and-data-mining agreement with
   a publisher, you may add an adapter for it in your fork; you, not this project, are responsible
   for its storage, retention, snippet and deletion terms.
8. **Two sources carry terms you must decide on.** KCI's OAI guide page forbids commercial use while
   KCI's data-services page licenses the same data for commercial use (KOGL type 1); the adapter is
   on by default and a commercial operator who does not rely on the type-1 reading sets
   `sources.kci.enabled: false`. J-STAGE forbids keeping API data in machine-readable form for 24
   hours or more and requires JST's approval for commercial use, so it is off by default; turning it
   on is your call and your risk, as with Google News (`docs/LEGAL.md` sections 4 and 5).
9. **Nothing here is legal advice.** Have counsel review `docs/LEGAL.md` before relying on it.
