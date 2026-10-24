"""Strict input parsing. Do not invent missing historic availability timestamps."""
from datetime import datetime, date, timedelta, timezone
import re

MISSING={'','-','--','N/A','nan','None','null'}

def number(x, *, required=False):
    if x is None or str(x).strip() in MISSING:
        if required: raise ValueError('missing required numeric value')
        return None
    s=str(x).strip().replace(',','').replace('%','').replace(' ','')
    if s.startswith('(') and s.endswith(')'): s='-'+s[1:-1]
    v=float(s)
    if not (-1e308<v<1e308): raise ValueError('non-finite numeric value')
    return v

def day(x):
    s=str(x or '').strip()
    for fmt in ('%Y-%m-%d','%Y%m%d','%d-%b-%Y','%Y/%m/%d','%Y.%m.%d'):
        try: return datetime.strptime(s,fmt).date().isoformat()
        except ValueError: pass
    raise ValueError(f'invalid date: {s!r}')

def parse_available(value=None):
    if value is None or not str(value).strip():
        from .db import utcnow
        return utcnow()
    s=str(value).strip()
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}',s):
        # End of named UTC date, conservative vs unknown intra-day publication.
        d=date.fromisoformat(s)+timedelta(days=1)
        return datetime.combine(d,datetime.min.time(),tzinfo=timezone.utc).isoformat()
    d=datetime.fromisoformat(s.replace('Z','+00:00'))
    if d.tzinfo is None: raise ValueError('available_at timestamp must include timezone offset')
    return d.astimezone(timezone.utc).isoformat()

def published_from_sec_date(filing_date):
    # Filing date known; use NEXT UTC date, not midnight of filing date.
    d=date.fromisoformat(day(filing_date))+timedelta(days=1)
    return datetime.combine(d,datetime.min.time(),tzinfo=timezone.utc).isoformat()
