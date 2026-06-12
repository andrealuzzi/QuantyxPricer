#!/usr/bin/env python3
"""
Simple termsheet -> asset JSON converter.

Usage:
  ./scripts/termsheet_to_json.py path/to/XS2464323760.pdf --out-dir assets/

This is a heuristic parser that extracts text from the PDF and looks for
ISIN, dates, coupon, currency, and simple metadata. It writes a JSON file
named by ISIN (or given --out-file) into the output directory.

Dependencies: PyPDF2 (pip install PyPDF2)
"""
import re
import json
from pathlib import Path
import argparse

try:
    from PyPDF2 import PdfReader
except Exception:
    PdfReader = None


def extract_text_from_pdf(path: Path) -> str:
    if PdfReader is None:
        raise RuntimeError('PyPDF2 not installed. Run: pip install PyPDF2')
    reader = PdfReader(str(path))
    texts = []
    for p in reader.pages:
        try:
            texts.append(p.extract_text() or '')
        except Exception:
            texts.append('')
    return "\n".join(texts)


ISIN_RE = re.compile(r"\b[A-Z]{2}[A-Z0-9]{9}\d\b")
PERCENT_RE = re.compile(r"(\d{1,2}(?:[\.,]\d+)?)\s?%")
DATE_RE1 = re.compile(r"(\d{1,2})[\-/](\d{1,2})[\-/](\d{2,4})")
DATE_RE2 = re.compile(r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})", re.I)
CURRENCY_RE = re.compile(r"\b(EUR|USD|GBP|CHF|JPY|AUD|CAD)\b")


def find_first_isin(text: str):
    m = ISIN_RE.search(text)
    return m.group(0) if m else None


def find_currency(text: str):
    m = CURRENCY_RE.search(text)
    return m.group(1) if m else None


def find_first_percent(text: str):
    m = PERCENT_RE.search(text)
    if not m:
        return None
    v = m.group(1).replace(',', '.')
    try:
        return float(v) / 100.0
    except Exception:
        return None


def find_dates(text: str):
    # return list of candidate dates (strings)
    dates = []
    for m in DATE_RE1.finditer(text):
        d, mo, y = m.groups()
        y = y if len(y) == 4 else ('20' + y)
        dates.append(f"{int(d):02d}-{int(mo):02d}-{y}")
    for m in DATE_RE2.finditer(text):
        d, mo, y = m.groups()
        months = { 'january': '01','february':'02','march':'03','april':'04','may':'05','june':'06','july':'07','august':'08','september':'09','october':'10','november':'11','december':'12'}
        mo_n = months[mo.lower()]
        dates.append(f"{int(d):02d}-{mo_n}-{y}")
    return dates


def heuristic_field_from_text(text: str):
    isin = find_first_isin(text)
    currency = find_currency(text)
    coupon = find_first_percent(text)
    dates = find_dates(text)

    # try to find maturity/issue by scanning for keywords nearby
    maturity = None
    issue = None
    lname = text.lower()
    # naive: look for 'maturity' or 'matures' and nearest date
    for key in ('maturity', 'matures', 'maturity date'):
        idx = lname.find(key)
        if idx >= 0:
            # take dates after this index
            after = find_dates(text[idx:])
            if after:
                maturity = after[0]
                break
    for key in ('issue date', 'issued on', 'issue'):
        idx = lname.find(key)
        if idx >= 0:
            after = find_dates(text[idx:])
            if after:
                issue = after[0]
                break

    # fallback: first/last date
    if not issue and dates:
        issue = dates[0]
    if not maturity and dates:
        maturity = dates[-1]

    # description: first non-empty line
    first_line = None
    for ln in text.splitlines():
        ln = ln.strip()
        if ln:
            first_line = ln
            break

    # model guess
    model = 'hullwhite'
    if 'credit-linked' in lname or 'cln' in lname or 'credit linked note' in lname:
        model = 'cln'

    return {
        'instrument_id': isin,
        'description': first_line,
        'currency': currency,
        'fixed_coupon_rate': coupon,
        'issue_date': issue,
        'maturity_date': maturity,
        'model': model,
        'par': 100.0,
    }


def process_file(pdf: Path, out_dir: Path, out_file: str = None):
    print(f'Processing {pdf}...')
    try:
        text = extract_text_from_pdf(pdf)
    except Exception as e:
        print(f'  Skipped (could not extract text): {e}')
        return
    fields = heuristic_field_from_text(text)
    out_dir.mkdir(parents=True, exist_ok=True)
    if not out_file:
        name = fields.get('instrument_id') or pdf.stem
        out_file = f"{name}.json"
    out_path = out_dir / out_file
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(fields, f, indent=2, ensure_ascii=False)
    print('  Wrote', out_path)


def main():
    p = argparse.ArgumentParser(description='Convert termsheet PDF(s) to asset JSON files')
    p.add_argument('path', nargs='?', default='termsheets', help='PDF file or directory to process (default: termsheets)')
    p.add_argument('--out-dir', default='assets', help='Output directory for asset JSON')
    p.add_argument('--pattern', default='*.pdf', help='Filename glob pattern when a directory is provided')
    p.add_argument('--recursive', action='store_true', help='Recursively scan directories')
    args = p.parse_args()

    src = Path(args.path)
    out_dir = Path(args.out_dir)

    if src.is_dir():
        if args.recursive:
            files = list(src.rglob(args.pattern))
        else:
            files = list(src.glob(args.pattern))
        if not files:
            print('No PDF files found in', src)
            return
        for f in sorted(files):
            process_file(f, out_dir)
    else:
        if not src.exists():
            print('Path not found:', src)
            return
        process_file(src, out_dir)


if __name__ == '__main__':
    main()
