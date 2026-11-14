"""Test-only launcher: real scripts, temporary checkout, offline provider/clock seams.

No production flag/environment variable enables this clock. Each invocation runs
in a new process; calendar collection also crosses its real worker boundary.
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


def main():
    root, clock, scenario, script, *arguments = sys.argv[1:]
    root = Path(root)
    sys.path[:0] = [str(root / 'src'), str(root / 'scripts')]
    os.chdir(root)
    instant = datetime.fromisoformat(clock)

    def offline(*args, **kwargs):
        raise AssertionError('network forbidden by automation integration test')

    socket.create_connection = offline
    socket.socket.connect = offline
    socket.socket.connect_ex = offline

    import pandas as pd

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    def download(symbol, *, start, end, **kwargs):
        fixture = json.loads((root / 'provider.json').read_text())
        rows = fixture.get(symbol, [])
        return pd.DataFrame({'Close': [row[1] for row in rows]},
                            index=pd.DatetimeIndex([row[0] for row in rows], name='Date'))

    sys.modules['yfinance'] = SimpleNamespace(download=download)
    spec = importlib.util.spec_from_file_location('_automation_cli_under_test', root / 'scripts' / script)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    if script == 'collect_economic_calendar.py':
        from roboquant.index_forecast import automation_state, economic_sources

        def fetch(url, timeout_seconds):
            if scenario == 'unavailable' or (scenario == 'partial' and 'empsit' in url):
                raise OSError('offline fixture provider unavailable')
            if scenario == 'timeout':
                import time
                time.sleep(10)
            return SimpleNamespace(status=200, headers={'Content-Type': 'text/html'}, body=b'''
                <h1>Consumer Price Index Summary</h1>
                <p>Transmission of material in this release is embargoed until
                8:30 a.m. (ET) Tuesday, November 10, 2026</p>
                <p>The Consumer Price Index for All Urban Consumers (CPI-U) increased
                0.4 percent on a seasonally adjusted basis in October 2026.</p>
                <p>The index for all items less food and energy rose 0.3 percent in October.</p>
            ''')

        original_collect = economic_sources.collect_economic_sources
        sources = [{
                'source_id': 'us_bls_cpi_current', 'country': 'US', 'kind': 'actual',
                'source_url': 'https://www.bls.gov/news.release/cpi.nr0.htm',
                'parser': 'bls_cpi', 'access': 'public_no_key', 'format': 'html'}]
        if scenario == 'partial':
            sources.append({**sources[0], 'source_id': 'us_bls_employment_current',
                            'source_url': 'https://www.bls.gov/news.release/empsit.nr0.htm',
                            'parser': 'bls_employment'})
        economic_sources.collect_economic_sources = lambda **kwargs: original_collect(
            **kwargs, fetch=fetch, registry={'sources': sources})
        original_run = automation_state.run_calendar_collection
        automation_state.run_calendar_collection = lambda output, private: original_run(
            output, private, budget_seconds=.2 if scenario == 'timeout' else 15,
            worker_command=[sys.executable, __file__, str(root), clock, scenario, script,
                            '--worker-result', '{result}'])

    # Modules use explicit datetime imports. Patch only our loaded modules, after
    # pandas imports, leaving third-party internals and monotonic deadlines real.
    for loaded in tuple(sys.modules.values()):
        if getattr(loaded, '__name__', '').startswith(('roboquant.index_forecast', '_automation')):
            if getattr(loaded, 'datetime', None) is datetime:
                loaded.datetime = FrozenClock
    if hasattr(module, 'state'):
        module.state.datetime = FrozenClock
    sys.argv = [str(root / 'scripts' / script), *arguments]
    result = module.main()
    if isinstance(result, int):
        raise SystemExit(result)


if __name__ == '__main__':
    main()
