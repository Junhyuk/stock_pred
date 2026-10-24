# Signal Agent
Implement conservative, explainable rules in `smartmoney/signals.py`. Default LOW uncertainty; classify with multiple independent observations, not arbitrary black-box purchase signals.
KR: 20 market trading days foreign net share purchase persistence + ownership percentage point change, with price check; never conflate positive net trade with specific foreign investor holding.
US: compare reported shares across 13F periods of same manager and verified CUSIP, not mark-to-market value. Reject unadjusted splits and amendments for strong conclusions. Missing position in archive is not automatically a sale.
Calculate all features after point-in-time published/ingested cutoff, not filing's report quarter alone. No success probabilities without out-of-sample calibration.
