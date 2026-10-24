# International news candidate: frozen protocol

This candidate covers KOSPI, KOSDAQ, US_SPX and US_NASDAQ independently. It cannot
approve production. Missing historical observation and feed-coverage evidence
requires numerical news mixture weight zero; current metadata still supplies
attributed risk context. No headline has a predefined positive or negative index
return. An authentic official statement proves its provenance, not the truth of
an underlying military claim.

The trial is frozen before new news-candidate metrics: alpha grid
`[0, 0.05, 0.10, 0.20]`, maximum `0.20`, fixed linear Ridge(10) and
LogisticRegression(C=1), median imputation and standard scaling fitted only on
earlier mature training rows. Features are fixed trailing 24-hour unique cluster
and source counts, confirmed escalation, reported escalation, reported
de-escalation, shipping/energy, trade/sanctions, monetary policy, uncertainty,
unconfirmed reporting and 24-hour versus preceding six-day novelty. These are
attention/topic features, not causal effect estimates. Topic rules are the
versioned literal bilingual rules in `international_news.py`; no narrative-driven
feature selection, LLM retrospective labels or future knowledge is used.

The shallow news expert needs 60 earlier mature, coverage-complete rows before
fitting. Each OOF prediction has its own preceding mature OOF probability
calibration and residual interval: 252-row window, minimum 60. Every mixture alpha
also receives its own preceding mature OOF sigmoid calibration and 80% absolute
residual order statistic. It never averages component bounds. An observation's
own label is excluded from its training, probability calibration and interval.
Per-market explicit cutoff, target close and label receipt clocks are mandatory;
calendar dates alone do not establish maturity. The caller supplies exchange
calendar boundaries and the frozen base candidate's own OOF records.

Only explicit earlier development split rows can select alpha. The unmodified
base is alpha zero. Ranking uses development Brier, log loss, MAE and then the
smaller alpha; selection additionally requires a negative upper bound of the
paired moving-block 95% Brier-loss-difference interval versus the same-date base.
The held-out terminal block cannot choose an alpha; it can veto the frozen
choice. At least 252 common-date calibrated terminal observations and the base
global quality gates are required for positive applied research alpha. The mixed
candidate must pass the existing probability, MAE, coverage, width and downside
gates against the aligned legacy/up-rate/mean baselines, plus incremental Brier
confidence and MAE gates against the frozen base. Prospective production approval
and its 60-day live-final requirement remain separate. Test-only smaller sample
settings demonstrate mechanics and establish no market efficacy.

Source attention uses fixed declared provenance priors (official 1.0, reporting
wire 0.8, public aggregator/unknown 0.5), an exponential 24-hour age half-life and
an unconfirmed-claim multiplier 0.5. These are editorial risk-attention weights,
not source correctness probabilities, fitted return weights or alpha. Duplicate
URLs/tracking variants and identical syndicated headline clusters count once.
Immutable versions keep original receipt clocks; revisions become usable only
after their own publication/update/receipt maximum. Date-only publication has
unknown publication time and uses actual receipt, never reconstructed midnight.
Receipt establishes availability, not publication freshness. Known old
publication dates exclude historical context from current features and attention.
Recent date-only items use an explicitly labelled conservative calendar-date age
bound and stay in a separate context bucket; their unknown intraday publication
clock cannot fill an exact 24-hour model feature. Entirely unknown publication age stays in a separate context bucket with
attention weight zero and makes numeric feature coverage unready. Novelty needs
seven complete trailing days of declared capture, not only a current feed head.

Feed acquisition uses only configured public RSS (Federal Reserve, UN News and
ECB) or the documented public GDELT
DOC ArtList endpoint, bounded requests and metadata (title, URL, source, timestamp,
topics, claim status). It stores no article body. GDELT `seendate` is a provider
crawl observation, not original publication. Actual local receipt is separate.
Failure, capped results and RSS heads do not prove a quiet or complete interval.
The default collector therefore records incomplete coverage; it never certifies
past continuous observation from a fresh request. Audited continuous coverage
windows must be supplied separately before numerical model readiness.

## Evidence card (2026-10-24 KST)

| Required evidence | Current evidence / limitation |
| --- | --- |
| Economic hypothesis | Shipping/energy, monetary, trade and geopolitical attention may add state information beyond prices; direction and size must be learned per market. |
| Acquisition | Configured Fed, UN News and ECB official RSS and documented GDELT DOC ArtList metadata; bounded current requests, no press-body crawler. |
| Actual publication time | Preserve timezone-aware publication/update time only when supplied; date-only or GDELT crawl time is not publisher time. Local first receipt is mandatory and never backdated. |
| Cost | Public endpoints require no configured paid key; network/maintenance cost and source rights still apply. No expensive model or LLM setup. |
| Missingness | Historical feed-coverage completeness unestablished; unknown intervals remain missing, not zero. |
| History length | Existing DB: market_news_feed 43 (published May14-Jul23, collected Jun21-Jul23); marketnewsfeed_headlines 159 (Jul10-Jul23); local_llm_news_documents 303; global_news_events and news_articles zero. These are metadata availability counts, not sufficient same-date OOF samples. |
| Leakage | Publication, update, actual first receipt and target maturity gates; no current-event backfill. The 58 local_llm_news_signal_daily rows created Oct4 do not establish original historical PIT features. |
| Baseline increment | Unmeasured on audited news history. Applied alpha zero until same-date development and held-out evidence pass all gates. Synthetic tests are not performance evidence. |
| Regimes | Year, volatility and trend slices are reported when validated OOF exists; current real-data result is unmeasured. |
| Ablation | Exact frozen base alpha0, news-only and predeclared mixtures use identical scored dates; current real-data incremental/ablation effects unmeasured. |

The bounded October 24 refresh captured 20 Fed, 30 UN News and 15 ECB metadata
items, plus six separately reviewed contextual records: 71 total local versions.
Fed response receipt was `2026-10-24T00:03:19.580176+00:00`; its timezone-naive
publisher clocks remain date-only. UN and ECB receipts were respectively
`00:06:48.984425Z` and `00:06:50.211031Z` on the same date. All 71 records retain
known actual receipt after correcting the Fed raw receipt representation; none
has a complete historical capture window. The documented GDELT request failed
with an SSL handshake timeout; unattempted queries remain missing. BBC RSS was
excluded after its official terms disallowed the intended metadata extraction.
Feed counts are collection evidence, not predictive training or efficacy evidence.

The collector already retains history across runs. Run
`python scripts/collect_international_news.py --output data/raw/international_news/metadata.json`
and keep using the same output path: it reads the previous materialized JSON,
retains earlier immutable event IDs and their actual receipt clocks, adds new
metadata versions, appends per-feed attempt/receipt windows (including failures),
then atomically replaces the aggregate file. It does not discard the previous
feed head. Changed titles, or source update timestamps where supplied in metadata, produce separate retained
versions; earlier versions remain available for historical cutoffs. An unchanged
item fetched again currently also produces a receipt-specific version because
the event ID includes first receipt; this is redundant storage rather than a
loss of history, and model input deduplicates the available story versions.
Repeating the exact same captured event ID does not duplicate its event row.
The current window list simply appends: an identical receipt reimport can repeat
a window entry, while a genuinely new poll properly records a new receipt and
version. Neither behavior silently overwrites an earlier observation.

This local raw JSON is ignored by Git and must be retained or backed up by the
operator. Changing `--output`, deleting it, or supplying only a newly fetched head
to a later run does not preserve that previous path's archive. Accumulated
immutable metadata and attempt windows support a future coverage audit; successful
polls alone never set `complete=true`, manufacture quiet zeros, or prove original
historical publication. The current 71-row snapshot remains unready with alpha
zero. No scheduler or new network collection was installed by this change.

Verification: delegated narrow RED established the module was initially absent;
the completed test file passed 12 tests with deprecation warnings treated as
errors, and targeted Ruff passed. Tests exercise a genuinely predictive synthetic
expert selecting positive bounded alpha, a stronger base rejecting a noise
expert, terminal outcome mutation leaving alpha selection and that observation's
issued residual interval unchanged, maturity, clocks, missing coverage, metadata
only acquisition, claims and syndication. These tests do not validate real market
predictive gain.

Current US–Iran context retains confirmed reporting, attributed remarks and
unverified incident claims separately. An unconfirmed vessel attack is not a
confirmed escalation. Missing feed capture or a bounded search finding no new
verified strike does not establish the absence of an event. Collect immutable
metadata and auditable coverage prospectively; do not manufacture an historical
feature series to force nonzero news inclusion.
