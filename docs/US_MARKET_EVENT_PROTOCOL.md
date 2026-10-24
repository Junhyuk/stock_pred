# US Market and Event Context Protocol

## Scope and market clocks

The research outputs cover KOSPI, KOSDAQ, S&P 500 and Nasdaq Composite. Each forecast uses its own target index history, mature labels, calibration, session calendar and next-session opening deadline. US outputs must not inherit an 08:00 KST snapshot or KRX opening deadline. Keep the index observation date, feature cutoff, actual run start, publication decision and target session separate.

Use the latest completed session for each input instrument at the actual cutoff. Equity-index and VIX closing times differ; account for time zones, daylight saving, holidays and early closes. An ongoing daily bar is unavailable as a completed close. A missing expected provider session is missing data, while a genuine exchange holiday permits the most recent completed session to remain current. A 1-day return requires consecutive instrument sessions, with no forward fill across missing observations.

## Frozen US feature trial

Before inspecting new terminal results, the trial fixed one `price_us` group: the existing seven price features plus `us_spx_return_1d`, `us_nasdaq_return_1d`, `us_sox_return_1d`, `us_vix_level`, `us_vix_return_1d` and `sox_nasdaq_spread_1d`. FX and rates are outside this trial. Model families and reliability thresholds remain fixed.

The frozen candidate matrix is KOSPI/KOSDAQ: `price` and `price_us` on common eligible dates; S&P 500/Nasdaq Composite: seven own-market price features only. A predeclared linear `price_us` challenger may be shown separately when ready, using its own mature out-of-fold calibration; it is not a replacement selected by terminal performance. Korean forecasts consume observed completed US bars, never the forecasted future US close.

Join US observations to each Korean prediction cutoff, not merely its Korean asof date. A Monday final snapshot may use the preceding completed US Friday session; an earlier preliminary snapshot before that US close may not. Compare price-only and price-plus-US candidates on the same eligible training and OOS dates, with aligned baselines. Missing live US data must produce an explicit unavailable group or declared price-only fallback.

Historical US close availability reconstructed from session calendars is retrospective evidence, not proof of original publication time or historical data versions. Actual collection/first-seen times are not backdated. The existing terminal history has already been viewed; this added-feature trial remains retrospective even if its statistical gates pass. Report the selected input group truthfully: attached US evidence does not mean the issued model used US features.

## US–Iran news context

Current news is a timestamped explanation and conditional risk overlay. Active forecasts currently use news `α = 0` because complete point-in-time feed-coverage windows are not established, so their output equals the base forecast exactly. A separate implemented news candidate uses deduplicated counts, topics, uncertainty and novelty with source/age attention weights; attention is not a signed price effect. The bounded `α` grid is `0`, `.05`, `.10` and `.20`; it is selected only on development using aligned out-of-fold comparisons and its own mixed calibration. Terminal rows may veto the selected value to `0`, but may never select a better value. Signed impact requires historical evidence; current headlines cause no manual probability adjustment. Each event retains source URL, publication/update time where known, actual first-seen time, attribution and verification status. Unknown timestamps are not invented; current articles are not backfilled into historical feature rows.

A verified publication does not independently verify all claims it reports. Keep observed reporting facts, attributed claims, unconfirmed information and scenario conditions separate. Deduplicate overlapping reports without combining uncertain incident counts. Official statements establish what an authority said; they do not automatically corroborate the underlying event. An absence of alerts is not proof that an incident did not happen.

The bounded source review was refreshed at 2026-10-23 23:58:53 UTC. It included [AP's October 22 shipping report](https://apnews.com/article/iran-united-states-war-hormuz-trump-israel-2abebce2fca2c085e0e0ee717e42cf28), which attributes Iranian vessel-incident claims and says it could not independently confirm the specific events; [AP's October 23 rally report](https://apnews.com/article/donald-trump-cuba-venezuela-florida-3b33aab6719558e289632e974c7a82ad); and [Reuters' October 23 report syndicated by Internazionale](https://www.internazionale.it/ultime-notizie-reuters/2026/10/23/trump-weighs-quick-end-to-iran-war-against-letting-conflict-wither), which reports Trump's remarks about ending the conflict quickly or letting it wind down and his claim that oil is flowing through Hormuz. The report verifies that he made the remarks; it does not independently establish a decision or shipping throughput. An October 20 Reuters dispatch was also read through [MarketScreener](https://uk.marketscreener.com/news/iran-prolongs-talks-in-stalemate-to-influence-us-midterms-officials-say-ce785cd8d98df420); its canonical Reuters URL was not verified. The bounded search did not find a newly verified October 23 direct strike; this is a search result, not a claim that no strike occurred.

Escalation, shipping disruption and de-escalation scenarios may describe conditional oil, volatility, currency and sector channels. They must not supply invented market-impact magnitudes or forecast-probability adjustments. Big-participant holdings and impact remain insufficient/inconclusive while actual participant observations are zero.

## Release and Git evidence

Keep software verification, publication validity, statistical acceptance, event verification and participant-data readiness as separate decisions. An issued view requires the matching publication receipt and payload checksums; a raw unpublished payload is not an issued forecast. Preserve actual timestamps and the declared timestamp basis.

Production weight remains 0 without the required independent approval and prospective evidence; the 60-day live-final requirement is not established by reconstructed history. Record same-date candidate metrics, gate failures, source/calendar provenance and news limitations. Do not tune thresholds after seeing the new terminal result.

Test the exact clean commit tree, with imports resolving to that tree. Stage only required source, tests, configuration, documentation and intentionally selected derived reports. Exclude credentials, environments, databases/WAL files, backups, raw downloads, full licensed articles and unrelated dirty-user changes. Push normally to the authorized feature branch and record the remote URL, commit SHA and matching remote branch SHA; do not force-push or alter hosting as an incidental step.
