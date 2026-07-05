"""Fetch daily US/HK market data + HK IPO data.

Runs on GitHub Actions (US servers) so yfinance is never blocked.
Outputs JSON to ../data/ — consumed locally via jsDelivr CDN.

Usage:
    python fetch_daily_feeds.py [--out DIR]

Env vars (optional):
    TZ=America/New_York    — or any IANA tz; UTC by default
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import requests
import yfinance as yf
from bs4 import BeautifulSoup

SCRIPT_DIR = Path(__file__).parent
REPO_DIR = SCRIPT_DIR.parent
DATA_DIR = REPO_DIR / "data"
CONFIG_DIR = REPO_DIR / "config"

REQUEST_TIMEOUT = 30
AASTOCKS_BASE = "https://www.aastocks.com"
AASTOCKS_CURRENT_IPO = f"{AASTOCKS_BASE}/en/IPOs/CurrentIPO.aspx"
AASTOCKS_RESULTS = f"{AASTOCKS_BASE}/en/IPOs/IPOResults.aspx"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

OUTPUT_FILES = {
    "us_market": "us-market.json",
    "hk_market": "hk-market.json",
    "hk_ipo": "hk-ipo.json",
}


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def warn(msg: str) -> None:
    print(f"[WARN] {msg}", file=sys.stderr, flush=True)


# ── Config ──────────────────────────────────────────────────────────────────────

def load_tickers() -> dict[str, list[dict[str, str]]]:
    path = CONFIG_DIR / "feed-tickers.json"
    if path.exists():
        return json.loads(path.read_text("utf-8"))
    warn(f"{path} not found; using defaults")
    return {
        "us": [{"ticker": "SPY", "name": "S&P 500 ETF"}],
        "hk": [{"ticker": "2800.HK", "name": "TraHK (HSI ETF)"}],
    }


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    return s


# ── Yahoo Finance helpers ───────────────────────────────────────────────────────

def _yf_batch(tickers: list[str]) -> dict[str, Any]:
    """Bulk download OHLCV data via yfinance."""
    if not tickers:
        return {}
    try:
        df = yf.download(
            tickers,
            period="2d",
            interval="1d",
            auto_adjust=True,
            progress=False,
            group_by="ticker",
        )
    except Exception as e:
        log(f"yfinance batch download failed: {e}")
        return {}

    if df.empty:
        return {}

    results: dict[str, Any] = {}
    for t in tickers:
        try:
            if len(tickers) == 1:
                tdf = df
            else:
                tdf = df[t]

            if tdf.empty or "Close" not in tdf.columns:
                continue

            row = tdf.iloc[-1]
            prev = tdf.iloc[-2] if len(tdf) >= 2 else row
            prev_close = float(prev["Close"]) if prev["Close"] == prev["Close"] else None
            close = float(row["Close"]) if row["Close"] == row["Close"] else None
            change_pct = (
                round((close - prev_close) / prev_close * 100, 2)
                if close is not None and prev_close and prev_close != 0
                else None
            )

            results[t] = {
                "price": close,
                "change_pct": change_pct,
                "open": float(row["Open"]) if row["Open"] == row["Open"] else None,
                "high": float(row["High"]) if row["High"] == row["High"] else None,
                "low": float(row["Low"]) if row["Low"] == row["Low"] else None,
                "volume": int(row["Volume"]) if row["Volume"] == row["Volume"] else None,
            }
        except (KeyError, IndexError, TypeError) as e:
            log(f"  error parsing {t}: {e}")
            continue

    return results


def _yf_info(ticker: str) -> dict[str, Any]:
    """Fetch metadata (name, market_cap, currency) for one ticker."""
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
        return {
            "short_name": info.get("shortName") or info.get("longName"),
            "market_cap": info.get("marketCap"),
            "currency": info.get("currency"),
        }
    except Exception:
        return {}


def fetch_us_market(tickers: list[dict[str, str]]) -> dict[str, Any]:
    symbols = [t["ticker"] for t in tickers]
    log(f"Fetching {len(symbols)} US stocks via yfinance...")
    prices = _yf_batch(symbols)

    result = []
    errors = []
    for entry in tickers:
        sym = entry["ticker"]
        p = prices.get(sym, {})
        if p.get("price") is None:
            errors.append(sym)
            continue
        meta = _yf_info(sym)
        result.append({
            "ticker": sym,
            "name": meta.get("short_name") or entry["name"],
            "price": p["price"],
            "change_pct": p["change_pct"],
            "open": p["open"],
            "high": p["high"],
            "low": p["low"],
            "volume": p["volume"],
            "market_cap": meta.get("market_cap"),
            "currency": meta.get("currency", "USD"),
        })

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(result),
        "data": result,
    }
    if errors:
        out["errors"] = f"{len(errors)} tickers failed: {', '.join(errors[:10])}"
        log(f"  {len(result)} ok, {len(errors)} failed")
    else:
        log(f"  {len(result)} stocks fetched")
    return out


def fetch_hk_market(tickers: list[dict[str, str]]) -> dict[str, Any]:
    symbols = [t["ticker"] for t in tickers]
    log(f"Fetching {len(symbols)} HK stocks via yfinance...")
    prices = _yf_batch(symbols)

    result = []
    errors = []
    for entry in tickers:
        sym = entry["ticker"]
        p = prices.get(sym, {})
        if p.get("price") is None:
            errors.append(sym)
            continue
        meta = _yf_info(sym)
        result.append({
            "ticker": sym.replace(".HK", ""),
            "name": meta.get("short_name") or entry["name"],
            "price": p["price"],
            "change_pct": p["change_pct"],
            "open": p["open"],
            "high": p["high"],
            "low": p["low"],
            "volume": p["volume"],
            "market_cap": meta.get("market_cap"),
            "currency": meta.get("currency", "HKD"),
        })

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(result),
        "data": result,
    }
    if errors:
        out["errors"] = f"{len(errors)} tickers failed: {', '.join(errors[:10])}"
        log(f"  {len(result)} ok, {len(errors)} failed")
    else:
        log(f"  {len(result)} stocks fetched")
    return out


# ── HK IPO scraper ──────────────────────────────────────────────────────────────

def _ipo_int(text: str | None) -> int | None:
    if not text:
        return None
    cleaned = re.sub(r"[^\d]", "", text)
    return int(cleaned) if cleaned else None


def _ipo_float(text: str | None) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"[^0-9.]", "", text)
    return float(cleaned) if cleaned else None


def _ipo_date(text: str | None) -> str | None:
    """Normalize a date string from AASTOCKS to YYYY-MM-DD."""
    if not text:
        return None
    text = text.strip()
    # AASTOCKS uses DD/MM/YYYY
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", text)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return text


def _parse_aastocks_table(html: str, url: str) -> list[dict[str, Any]]:
    """Parse the IPO table from AASTOCKS."""

    soup = BeautifulSoup(html, "lxml")

    # Find the main IPO table — various possible IDs
    table = (
        soup.find("table", id=lambda x: x and "gvIPO" in x)
        or soup.find("table", class_=lambda x: x and "ipo" in x.lower() if x else False)
        or soup.find("table", {"border": "0", "cellpadding": "3"})
    )
    if not table:
        # fallback: first large table with >3 rows
        for t in soup.find_all("table"):
            rows = t.find_all("tr")
            if len(rows) >= 3 and len(rows[0].find_all(["th", "td"])) >= 4:
                table = t
                break

    if not table:
        log(f"  no table found on {url}")
        return []

    rows = table.find_all("tr")
    if len(rows) < 2:
        return []

    # Detect header row
    header_row = rows[0]
    headers = [th.get_text(strip=True).lower() for th in header_row.find_all(["th", "td"])]

    ipos = []
    for row in rows[1:]:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        row_data: dict[str, str] = {}
        for i, cell in enumerate(cells):
            if i < len(headers):
                row_data[headers[i]] = cell.get_text(" ", strip=True)
            else:
                row_data[f"col_{i}"] = cell.get_text(" ", strip=True)

        # Try to extract stock code — usually in a link
        code = None
        for cell in cells[:2]:
            link = cell.find("a")
            if link:
                href = link.get("href", "")
                m = re.search(r"[&?]s=(\d{4,5})", href)
                if m:
                    code = m.group(1)

        name = row_data.get("stock name") or row_data.get("company") or row_data.get("col_0", "")

        ipos.append({
            "code": code,
            "name": name,
            "offer_price_low": _ipo_float(row_data.get("offer price ($)", "")),
            "offer_price_high": _ipo_float(row_data.get("offer price", "")),
            "lot_size": _ipo_int(row_data.get("lot size", "")),
            "min_subscription": _ipo_float(row_data.get("min. subscription", "")),
            "subscription_start": _ipo_date(row_data.get("subscription start", "")),
            "subscription_end": _ipo_date(row_data.get("subscription end", "")),
            "listing_date": _ipo_date(row_data.get("listing date", "")),
            "sponsor": row_data.get("sponsor / underwriter", ""),
            "status": row_data.get("status", ""),
            "source": url,
        })

    return ipos


def fetch_hk_ipo() -> dict[str, Any]:
    """Fetch current HK IPO information."""
    log("Fetching HK IPO data from AASTOCKS...")

    ipos: list[dict[str, Any]] = []
    errors: list[str] = []

    sess = session()

    for name, url in [
        ("current_ipo", AASTOCKS_CURRENT_IPO),
        ("results", AASTOCKS_RESULTS),
    ]:
        try:
            resp = sess.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            items = _parse_aastocks_table(resp.text, url)
            log(f"  {name}: {len(items)} items")
            for item in items:
                item["ipo_type"] = name
            ipos.extend(items)
        except Exception as e:
            msg = f"{name}: {e}"
            errors.append(msg)
            warn(msg)

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(ipos),
        "data": ipos,
    }
    if errors:
        out["errors"] = errors
    return out


# ── Main ────────────────────────────────────────────────────────────────────────

def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"  wrote {path.name} ({len(json.dumps(data)):,} bytes)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=str, default=str(DATA_DIR))
    parser.add_argument("--us-only", action="store_true")
    parser.add_argument("--hk-only", action="store_true")
    parser.add_argument("--ipo-only", action="store_true")
    args = parser.parse_args()

    data_dir = Path(args.out)
    tickers = load_tickers()

    all_pass = not (args.us_only or args.hk_only or args.ipo_only)

    if all_pass or args.us_only:
        us = fetch_us_market(tickers.get("us", []))
        write_json(data_dir / OUTPUT_FILES["us_market"], us)

    if all_pass or args.hk_only:
        hk = fetch_hk_market(tickers.get("hk", []))
        write_json(data_dir / OUTPUT_FILES["hk_market"], hk)

    if all_pass or args.ipo_only:
        ipo = fetch_hk_ipo()
        write_json(data_dir / OUTPUT_FILES["hk_ipo"], ipo)

    log("Done.")


if __name__ == "__main__":
    main()
