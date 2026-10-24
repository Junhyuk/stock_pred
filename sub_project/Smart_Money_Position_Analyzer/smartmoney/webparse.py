"""Public static HTML table -> explicit CSV/DB. No JS execution or anti-bot bypass."""
from __future__ import annotations
import csv
from html.parser import HTMLParser
import io
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
from .collectors import fetch_bytes
from .importers import import_text

AGENT='SmartMoneyResearch/0.1 (public permitted HTML only)'

class TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables=[];self.depth=0;self.rows=None;self.row=None;self.cell=None;self.headers=[]
    def handle_starttag(self,tag,attrs):
        if tag=='table':
            if self.depth==0: self.rows=[]
            self.depth+=1
        elif self.depth==1 and tag=='tr': self.row=[]
        elif self.depth==1 and tag in ('td','th') and self.row is not None:
            attr=dict(attrs)
            if int(attr.get('colspan','1'))!=1 or int(attr.get('rowspan','1'))!=1:
                raise ValueError('HTML table rowspan/colspan unsupported: manual cleanup required')
            self.cell=[]
        elif self.cell is not None and tag=='br':self.cell.append(' ')
    def handle_data(self,data):
        if self.cell is not None:self.cell.append(data)
    def handle_endtag(self,tag):
        if tag in ('th','td') and self.cell is not None and self.row is not None:
            self.row.append(' '.join(''.join(self.cell).split()))
            self.cell=None
        elif tag=='tr' and self.row is not None and self.rows is not None:
            if self.row:self.rows.append(self.row)
            self.row=None
        elif tag=='table' and self.depth:
            self.depth-=1
            if self.depth==0:
                self.tables.append(self.rows)
                self.rows=None


def _check_robots(url,allowed_host):
    # If robots.txt unavailable, fail closed for web scraping (offline CSV is alternative).
    from urllib.request import Request,build_opener
    from .collectors import SecureRedirect
    robot_url=f'https://{allowed_host}/robots.txt'
    try:
        raw=fetch_bytes(robot_url,{allowed_host},user_agent=AGENT,max_bytes=1_000_000)
    except Exception as exc:
        raise PermissionError('Cannot verify robots.txt; use manual CSV export instead') from exc
    rp=RobotFileParser(robot_url)
    rp.parse(raw.decode('utf-8',errors='replace').splitlines())
    if not rp.can_fetch(AGENT,url): raise PermissionError('robots.txt disallows automated access')


def public_html_table(url,allow_host,table_index=0,*,confirm_rights=False):
    if not confirm_rights: raise PermissionError('Pass --confirm-rights after checking site TOS and robots rules')
    parsed=urlparse(url)
    if parsed.hostname!=allow_host: raise ValueError('Exact --allow-host required; no wildcards or IP shortcuts')
    _check_robots(url,allow_host)
    raw=fetch_bytes(url,{allow_host},user_agent=AGENT,max_bytes=7_000_000)
    for charset in ('utf-8-sig','cp949','euc-kr'):
        try: html=raw.decode(charset);break
        except UnicodeDecodeError: continue
    else: raise ValueError('Unsupported HTML encoding')
    parser=TableParser();parser.feed(html)
    if not parser.tables: raise ValueError('No static HTML tables found; use official CSV/API instead of JS/private endpoints')
    if table_index>=len(parser.tables): raise ValueError(f'Table index too large; found {len(parser.tables)} tables')
    table=parser.tables[table_index]
    if len(table)<2: raise ValueError('Table has no header/data rows')
    headers=table[0]
    if len(set(headers))!=len(headers): raise ValueError('Non-unique headers; export CSV and normalize manually')
    if any(len(row)!=len(headers) for row in table[1:]):
        raise ValueError('Inconsistent HTML table columns; stop rather than shift values')
    output=io.StringIO();w=csv.writer(output);w.writerow(headers);w.writerows(table[1:])
    return output.getvalue(), {'tables_found':len(parser.tables),'headers':headers,'rows':len(table)-1}


def import_web_table(conn,url,allowed_host,kind,*,mapping,market='KR',investor=None,
                     published_at=None,table_index=0,confirm_rights=False):
    text,meta=public_html_table(url,allowed_host,table_index,confirm_rights=confirm_rights)
    result=import_text(conn,kind,text,source='WEB_TABLE',source_url=url,
                       market=market,investor=investor,mapping=mapping,published_at=published_at)
    return {**result,**meta}
