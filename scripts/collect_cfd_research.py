#!/usr/bin/env python3
"""Import locally obtained KOFIA PDF disclosures for private CFD research."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from html.parser import HTMLParser
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
KOFIA_INDEX_URL = "https://www.kofia.or.kr/brd/m_198/list.do"
MAX_PDF_BYTES = 25 * (1 << 20)
ARCHIVE_TITLE = re.compile(r"종목별\s*CFD\s*잔고동향[_ ](\d{8})")
HEADER = re.compile(r"종목코드\s+종목명\s+시장구분\s+잔고수량\s*(?:㈜|\(주\))\s+잔고금액\s*\(만원\)")
DATE_HEADER = re.compile(r"[.\s]*기준일자\s*:\s*(\d{4}-\d{2}-\d{2})")
INTEGER = r"(?:\d{1,3}(?:,\d{3})+|\d+)"
ROW = re.compile(rf"([0-9A-Z]{{6}})\s+(.+?)\s+(KOSPI|KOSDAQ)\s+({INTEGER})\s+({INTEGER})")
OBSERVATION_COLUMNS = (
    "observation_date", "posted_date", "published_at", "received_at", "retrospective_available_at", "market"
)
SCHEMA = """
CREATE TABLE IF NOT EXISTS cfd_source_documents (
    document_ref TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    source_url TEXT NOT NULL,
    attachment_url TEXT NOT NULL,
    observation_date TEXT NOT NULL,
    posted_date TEXT NOT NULL,
    received_at TEXT NOT NULL,
    raw_path TEXT NOT NULL,
    PRIMARY KEY (document_ref, sha256)
);
CREATE TABLE IF NOT EXISTS cfd_observations (
    document_ref TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    ticker TEXT NOT NULL,
    name TEXT NOT NULL,
    market TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    value_krw INTEGER NOT NULL CHECK (value_krw >= 0),
    PRIMARY KEY (document_ref, sha256, ticker),
    FOREIGN KEY (document_ref, sha256) REFERENCES cfd_source_documents(document_ref, sha256)
);
CREATE TEMP VIEW cfd_research_rows AS
SELECT d.observation_date, d.posted_date, NULL AS published_at, d.received_at,
       CASE WHEN (SELECT COUNT(DISTINCT v.sha256) FROM cfd_source_documents AS v
                  WHERE v.observation_date = d.observation_date AND v.posted_date = d.posted_date) = 1
            THEN retrospective_available_at(d.posted_date) END AS retrospective_available_at,
       o.market, o.ticker, o.name, o.value_krw, o.quantity, o.sha256 AS document_sha256
FROM cfd_observations AS o JOIN cfd_source_documents AS d USING (document_ref, sha256);
"""


@dataclass(frozen=True)
class Observation:
    ticker: str
    name: str
    market: str
    quantity: int
    value_krw: int


@dataclass(frozen=True)
class Disclosure:
    document_ref: str
    observation_date: str
    posted_date: str
    source_url: str
    attachment_url: str


@dataclass
class ArchiveCell:
    fragments: list[str]
    links: list[str]
    pdf_links: list[str]

    @property
    def text(self) -> str:
        return "".join(self.fragments).strip()


class ArchiveParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[ArchiveCell]] = []
        self.row: list[ArchiveCell] | None = None
        self.cell: ArchiveCell | None = None
        self.href: str | None = None

    def handle_starttag(self, tag, attrs) -> None:
        attrs = dict(attrs)
        if tag == "tr":
            self.row = []
        elif tag == "td" and self.row is not None:
            self.cell = ArchiveCell([], [], [])
        elif tag == "a" and self.cell is not None and (href := attrs.get("href")):
            self.href = href
            self.cell.links.append(href)
        elif tag == "img" and self.cell is not None and self.href and (attrs.get("src") or "").endswith("/pdf.gif"):
            self.cell.pdf_links.append(self.href)

    def handle_data(self, data) -> None:
        if self.cell is not None:
            self.cell.fragments.append(data)

    def handle_endtag(self, tag) -> None:
        if tag == "a":
            self.href = None
        elif tag == "td" and self.cell is not None and self.row is not None:
            self.row.append(self.cell)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def _official_ref(url: str, kind: str) -> str | None:
    parts = urlsplit(url)
    refs = parse_qs(parts.query).get("seq", [])
    if (parts.scheme == "https" and parts.netloc == "www.kofia.or.kr"
            and parts.path == f"/brd/m_198/{kind}.do" and len(refs) == 1 and re.fullmatch(r"[0-9]+", refs[0])):
        return refs[0]
    return None


def discover_disclosures(html: str, *, limit: int = 2) -> list[Disclosure]:
    """Read at most `limit` latest posts from the official archive listing."""
    if limit < 1:
        raise ValueError("Disclosure limit must be positive")
    parser = ArchiveParser()
    parser.feed(html)
    parser.close()
    disclosures = []
    seen = set()
    for cells in parser.rows:
        if len(cells) != 4 or (title := ARCHIVE_TITLE.fullmatch(cells[1].text)) is None:
            continue
        if len(cells[1].links) != 1:
            raise ValueError("Expected exactly one official disclosure detail link")
        source_url = urljoin(KOFIA_INDEX_URL, cells[1].links[0])
        document_ref = _official_ref(source_url, "view")
        if document_ref is None or document_ref in seen:
            raise ValueError("Invalid or duplicate official disclosure detail link")
        if len(cells[2].pdf_links) != 1:
            raise ValueError("Expected exactly one official attachment")
        attachment_url = urljoin(KOFIA_INDEX_URL, cells[2].pdf_links[0])
        if _official_ref(attachment_url, "down") != document_ref:
            raise ValueError("Invalid official attachment link")
        observation_date = date.fromisoformat(title[1])
        posted_date = date.fromisoformat(cells[3].text)
        if posted_date < observation_date:
            raise ValueError("Posting predates the observation date")
        seen.add(document_ref)
        disclosures.append(Disclosure(document_ref, observation_date.isoformat(), posted_date.isoformat(),
                                      source_url, attachment_url))
        if len(disclosures) >= limit:
            break
    if not disclosures:
        raise ValueError("No disclosures found in official archive")
    return disclosures


def parse_document(layout: str, observation_date: str) -> list[Observation]:
    observations = {}
    for page_no, page in enumerate(layout.split("\f"), 1):
        if not page.strip():
            continue
        dates = []
        header_count = 0
        row_count = 0
        for line_no, line in enumerate(page.splitlines(), 1):
            line = line.strip()
            if not line or set(line) <= {".", " "}:
                continue
            if found_date := DATE_HEADER.fullmatch(line):
                dates.append(found_date[1])
                continue
            if HEADER.fullmatch(line):
                header_count += 1
                continue
            found_row = ROW.fullmatch(line)
            if found_row is None:
                raise ValueError(f"Unrecognized source row at page {page_no}, line {line_no}")
            ticker, name, market, quantity, amount = found_row.groups()
            if ticker in observations:
                raise ValueError(f"Found duplicate ticker {ticker} at page {page_no}, line {line_no}")
            observations[ticker] = Observation(
                ticker, name, market, int(quantity.replace(",", "")), int(amount.replace(",", "")) * 10_000,
            )
            row_count += 1
        if dates != [observation_date]:
            raise ValueError(f"Wrong or missing observation date on page {page_no}")
        if header_count != 1 or row_count == 0:
            raise ValueError(f"Wrong or missing amounts header or rows on page {page_no}")
    if not observations:
        raise ValueError("No rows in document")
    return list(observations.values())


def parse_pdf(data: bytes, observation_date: str) -> list[Observation]:
    if len(data) > MAX_PDF_BYTES or not data.startswith(b"%PDF-"):
        raise ValueError("Source did not provide a PDF within the size limit")
    layout = subprocess.run(
        ["pdftotext", "-layout", "-enc", "UTF-8", "-", "-"], input=data,
        capture_output=True, check=True, timeout=30,
    ).stdout.decode("utf-8")
    return parse_document(layout, observation_date)


def retrospective_available_at(posted_date: str) -> str:
    day_after_post = date.fromisoformat(posted_date) + timedelta(days=1)
    return datetime.combine(day_after_post, time.min, KST).astimezone(UTC).isoformat()


def import_document(conn: sqlite3.Connection, directory: Path, entry: dict) -> None:
    doc_ref = entry["document_ref"]
    if not isinstance(doc_ref, str) or not doc_ref.isascii() or not doc_ref.isdecimal():
        raise ValueError("Invalid disclosure reference")
    for key, kind in (("source_url", "view"), ("attachment_url", "down")):
        if _official_ref(entry[key], kind) != doc_ref:
            raise ValueError(f"Invalid official {key}")
    asof = date.fromisoformat(entry["observation_date"])
    posted = date.fromisoformat(entry["posted_date"])
    received = datetime.fromisoformat(entry["received_at"])
    if received.tzinfo is None or not asof <= posted <= received.astimezone(KST).date():
        raise ValueError("Inconsistent observation, posting or receipt times")
    source_file = entry["file"]
    if Path(source_file).name != source_file or Path(source_file).suffix != ".pdf":
        raise ValueError("Expected a local PDF filename")
    pdf = (directory / source_file).resolve(strict=True)
    if pdf.parent != directory.resolve():
        raise ValueError("PDF must be in the manifest directory")
    with pdf.open("rb") as handle:
        data = handle.read(MAX_PDF_BYTES + 1)
    sha256 = hashlib.sha256(data).hexdigest()
    if conn.execute(
        "SELECT 1 FROM cfd_source_documents WHERE document_ref = ? AND sha256 = ?", (doc_ref, sha256)
    ).fetchone():
        return
    observations = parse_pdf(data, asof.isoformat())
    with conn:
        inserted = conn.execute(
            "INSERT INTO cfd_source_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (doc_ref, sha256, len(data), entry["source_url"], entry["attachment_url"],
             asof.isoformat(), posted.isoformat(), received.astimezone(UTC).isoformat(), str(pdf)),
        )
        if not inserted.rowcount:
            return
        conn.executemany(
            "INSERT INTO cfd_observations VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(doc_ref, sha256, row.ticker, row.name, row.market, row.quantity, row.value_krw) for row in observations],
        )


def write_csv(conn: sqlite3.Connection, path: Path, query: str) -> None:
    rows = conn.execute(query)
    with NamedTemporaryFile(mode="w", dir=path.parent, newline="", encoding="utf-8", delete=False) as output:
        writer = csv.writer(output)
        writer.writerow([column[0] for column in rows.description])
        writer.writerows(rows)
    Path(output.name).replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path, help="Private output directory under reports/")
    args = parser.parse_args()
    destination = args.output_dir.resolve()
    if "reports" not in destination.parts:
        parser.error("private research output belongs under reports/")
    entries = json.loads(args.manifest.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not entries:
        parser.error("manifest must contain at least one disclosure")
    destination.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(destination / "domestic_cfd.sqlite")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.create_function("retrospective_available_at", 1, retrospective_available_at, deterministic=True)
    conn.executescript(SCHEMA)
    failures = []
    try:
        for entry in entries:
            try:
                import_document(conn, args.manifest.parent, entry)
            except (KeyError, OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
                failures.append({"document_ref": entry.get("document_ref") if isinstance(entry, dict) else None,
                                 "reason": str(exc)})
        group_cols = ", ".join(OBSERVATION_COLUMNS)
        write_csv(conn, destination / "domestic_market.csv", f"""
            SELECT {group_cols}, SUM(value_krw) AS value_krw, SUM(quantity) AS quantity,
                   COUNT(*) AS valid_count, document_sha256
            FROM cfd_research_rows GROUP BY {group_cols}, document_sha256
            ORDER BY observation_date, received_at, market
        """)
        write_csv(conn, destination / "domestic_symbols.csv", """
            SELECT * FROM cfd_research_rows ORDER BY observation_date, received_at, market, ticker
        """)
        count = conn.execute("SELECT COUNT(*) FROM cfd_source_documents").fetchone()[0]
        audit = {"stored_documents": count, "quarantined": failures}
        (destination / "import_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(audit, ensure_ascii=False))
    finally:
        conn.close()
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
