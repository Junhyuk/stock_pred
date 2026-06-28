from __future__ import annotations

from pathlib import Path

import pandas as pd


def parse_saved_html_tables(path: str | Path) -> pd.DataFrame:
    """Parse saved public HTML tables into one raw DataFrame.

    This does not perform network collection. It only parses user-provided HTML fixtures.
    """
    tables = pd.read_html(Path(path))
    if not tables:
        return pd.DataFrame()
    frames = []
    for table in tables:
        frame = table.copy()
        frame.columns = [str(column).strip() for column in frame.columns]
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)

