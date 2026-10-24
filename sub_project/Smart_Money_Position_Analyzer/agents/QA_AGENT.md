# QA Agent
Automate: unit tests for CSV aliases, malformed dates/units, duplicate imports, SQLite counts, published cutoff, SEC ZIP 2023 dollar units, CFTC TFF schema, HTML parser, no fake populated data.
CI must be network-independent. Validate `python3 -m unittest discover -s tests -v`, CLI `doctor/status`, pre-release checklist and no key leaks.
Read back 5 randomly sampled observations from real permitted data later and compare against official source pages/downloads with date/unit. Mark unverified as `pending`, not pass.
Pass/fail must include evidence. Do not silently skip absent licensed data.
