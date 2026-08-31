#!/usr/bin/env python3
"""
Ante 9 — options levels archive (t-0352).

Snapshots the SPX option chain every few minutes through the session, derives gamma
structure from it, and writes three things to R2:

  1. raw/SPX/YYYY-MM-DD/YYYYMMDDTHHMMSSZ.json.gz
     The filtered chain, exactly as the source returned it, plus a provenance header.
     THIS IS THE ASSET. Intraday chain snapshots cannot be bought back later. Every
     snapshot is kept; nothing is ever overwritten.

  2. levels/SPX/YYYY-MM-DD.csv
     Derived levels as a TIME SERIES — one block of rows per snapshot, carrying the
     snapshot clock and the source. Appended, never replaced.

  3. bookmap/ES.csv, bookmap/MES.csv        (best-effort, --bookmap)
     Current-state only, in Bookmap cloud-notes format, basis-adjusted from SPX cash
     into futures points so Ante 9 can poll them like it polls pini-bot.

The archive (1 and 2) is written FIRST and stays in SPX cash points. The futures
conversion is downstream and best-effort: a bad basis or a dead quote source can
never contaminate the archive.

Source: Cboe delayed quotes (free, public, ~15 min delayed).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import io
import json
import os
import re
import sys
from collections import defaultdict
from typing import Any

import boto3
import requests
from botocore.config import Config

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/_{sym}.json"
SOURCE_ID = "cboe-delayed-quotes"

# OCC-style contract symbol: ROOT + YYMMDD + C/P + strike*1000, zero-padded to 8.
OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")

CONTRACT_MULTIPLIER = 100  # SPX index options

# Bookmap cloud-notes columns, matching what pini-bot already serves.
BM_HEADER = [
    "Symbol",
    "Price Level",
    "Note",
    "Foreground Color",
    "Background color",
    "Text Alignment",
    "Draw Note Price Horizontal Line",
]

# Archive columns: the settled Ante 9 level format (Symbol,Level,Type,Strength,
# Expiration,Updated) plus the provenance the archive needs to be worth anything
# in three months — which source, which clock, and what spot it was derived at.
ARCHIVE_HEADER = [
    "Symbol",
    "Level",
    "Type",
    "Strength",
    "Expiration",
    "Updated",
    "SnapshotUTC",
    "SourceTimestamp",
    "Source",
    "Spot",
    "GexNotional",
]

LEVEL_COLORS = {
    "GammaFlip": ("#FFFFFF", "#B026FF"),
    "CallWall": ("#FFFFFF", "#047A04"),
    "PutWall": ("#FFFFFF", "#FF0000"),
    "PositiveGamma": ("#FFFFFF", "#0B6E4F"),
    "NegativeGamma": ("#FFFFFF", "#8B0000"),
    "GammaWall": ("#FFFFFF", "#5151F1"),
}


# --------------------------------------------------------------------------- fetch


def fetch_chain(symbol: str, timeout: int = 45) -> tuple[dict[str, Any], dt.datetime]:
    """Fetch the chain. Returns (payload, our own UTC fetch time)."""
    url = CBOE_URL.format(sym=symbol)
    resp = requests.get(url, timeout=timeout, headers={"User-Agent": "ante9-gex-archive/1.0"})
    resp.raise_for_status()
    fetched_at = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    return resp.json(), fetched_at


def parse_contracts(payload: dict[str, Any], max_dte: int, strike_pct: float,
                    session_date: dt.date) -> tuple[list[dict[str, Any]], float]:
    """Filter to near-dated contracts near the money and attach parsed fields."""
    data = payload["data"]
    spot = float(data["current_price"])
    lo, hi = spot * (1 - strike_pct), spot * (1 + strike_pct)

    out = []
    for c in data["options"]:
        m = OCC.match(c["option"])
        if not m:
            continue
        root, yymmdd, cp, strike_raw = m.groups()
        strike = int(strike_raw) / 1000.0
        if not (lo <= strike <= hi):
            continue
        expiry = dt.date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
        dte = (expiry - session_date).days
        if dte < 0 or dte > max_dte:
            continue
        rec = dict(c)
        rec["_root"] = root
        rec["_expiry"] = expiry.isoformat()
        rec["_dte"] = dte
        rec["_right"] = cp
        rec["_strike"] = strike
        out.append(rec)
    return out, spot


# ----------------------------------------------------------------------- gex math


def strike_gex(contracts: list[dict[str, Any]], spot: float,
               dte_filter: int | None) -> dict[float, dict[str, float]]:
    """
    Net gamma exposure per strike, in dollars per 1% move in spot.

        gex = gamma * open_interest * 100 * spot^2 * 0.01

    Sign convention is the standard dealer-positioning assumption: dealers are
    assumed LONG calls and SHORT puts, so call gamma is positive and put gamma is
    negative. It is an assumption, not an observation — see README.
    """
    per: dict[float, dict[str, float]] = defaultdict(
        lambda: {"call": 0.0, "put": 0.0, "net": 0.0, "call_oi": 0.0, "put_oi": 0.0}
    )
    scale = CONTRACT_MULTIPLIER * spot * spot * 0.01

    for c in contracts:
        if dte_filter is not None and c["_dte"] != dte_filter:
            continue
        gamma = c.get("gamma")
        oi = c.get("open_interest")
        if gamma is None or oi is None:
            continue
        notional = float(gamma) * float(oi) * scale
        bucket = per[c["_strike"]]
        if c["_right"] == "C":
            bucket["call"] += notional
            bucket["call_oi"] += float(oi)
            bucket["net"] += notional
        else:
            bucket["put"] += notional
            bucket["put_oi"] += float(oi)
            bucket["net"] -= notional
    return dict(per)


def gamma_flip(per_strike: dict[float, dict[str, float]]) -> float | None:
    """
    Strike where cumulative net GEX crosses zero, walking strikes upward.

    PROXY, not a true zero-gamma spot solve — a real one recomputes every contract's
    gamma at each hypothetical spot. This is the cheap cumulative-crossing estimate
    that most public GEX dashboards use. Labelled as a proxy everywhere it surfaces
    so it never reads as more precise than it is (t-0314).
    """
    if not per_strike:
        return None
    strikes = sorted(per_strike)
    cum = 0.0
    prev_strike, prev_cum = None, None
    for k in strikes:
        cum += per_strike[k]["net"]
        if prev_cum is not None and (prev_cum < 0 <= cum or prev_cum > 0 >= cum):
            span = cum - prev_cum
            if span == 0:
                return k
            # linear interpolation between the two bracketing strikes
            return round(prev_strike + (0 - prev_cum) / span * (k - prev_strike), 2)
        prev_strike, prev_cum = k, cum
    return None


def derive_levels(per_strike: dict[float, dict[str, float]], top_n: int) -> list[dict[str, Any]]:
    """Turn the per-strike GEX profile into the level rows Ante 9 speaks."""
    if not per_strike:
        return []

    levels: list[dict[str, Any]] = []
    # One shared scale across all families, so a CallWall's 0.8 and a GammaWall's 0.4
    # mean the same thing. Normalising each family against its own max would make
    # every snapshot's biggest call wall read 1.00 regardless of how big it actually is.
    peak = max(
        max(v["call"] for v in per_strike.values()),
        max(v["put"] for v in per_strike.values()),
        max(abs(v["net"]) for v in per_strike.values()),
    ) or 1.0

    def row(level: float, kind: str, notional: float, strength: float | None = None) -> dict[str, Any]:
        return {
            "level": round(level, 2),
            "type": kind,
            "strength": round(min(abs(notional) / peak, 1.0), 3) if strength is None else strength,
            "gex": round(notional, 2),
        }

    flip = gamma_flip(per_strike)
    if flip is not None:
        # GammaFlip is a location, not a magnitude — it has no GEX size of its own.
        # Fixed at 1.0 rather than faking a strength that would read as measured.
        levels.append(row(flip, "GammaFlip", 0.0, strength=1.0))
    else:
        print("  no GammaFlip: cumulative net GEX does not cross zero inside the strike band")

    call_wall = max(per_strike.items(), key=lambda kv: kv[1]["call"])
    if call_wall[1]["call"] > 0:
        levels.append(row(call_wall[0], "CallWall", call_wall[1]["call"]))

    put_wall = max(per_strike.items(), key=lambda kv: kv[1]["put"])
    if put_wall[1]["put"] > 0:
        levels.append(row(put_wall[0], "PutWall", -put_wall[1]["put"]))

    pos = max(per_strike.items(), key=lambda kv: kv[1]["net"])
    if pos[1]["net"] > 0:
        levels.append(row(pos[0], "PositiveGamma", pos[1]["net"]))

    neg = min(per_strike.items(), key=lambda kv: kv[1]["net"])
    if neg[1]["net"] < 0:
        levels.append(row(neg[0], "NegativeGamma", neg[1]["net"]))

    claimed = {lv["level"] for lv in levels}
    ranked = sorted(per_strike.items(), key=lambda kv: abs(kv[1]["net"]), reverse=True)
    added = 0
    for strike, vals in ranked:
        if added >= top_n:
            break
        if strike in claimed:
            continue
        levels.append(row(strike, "GammaWall", vals["net"]))
        claimed.add(strike)
        added += 1

    return levels


# ------------------------------------------------------------------------ storage


class R2:
    """R2 via the S3 API, with two offline modes so the pipeline stays testable.

    dry_run  — derive everything, write nothing.
    out_dir  — write the same keys to a local directory tree instead of R2.
    """

    def __init__(self, bucket: str, dry_run: bool = False, out_dir: str | None = None):
        self.bucket = bucket
        self.out_dir = out_dir
        self.dry_run = dry_run or bool(out_dir)
        if self.dry_run:
            self.client = None
            return
        account = os.environ["R2_ACCOUNT_ID"]
        self.client = boto3.client(
            "s3",
            endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
            region_name="auto",
        )

    def put(self, key: str, body: bytes, content_type: str) -> None:
        if self.out_dir:
            path = os.path.join(self.out_dir, key)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(body)
            print(f"  WROTE {path} ({len(body):,} bytes)")
            return
        if self.dry_run:
            print(f"  [dry-run] would PUT {key} ({len(body):,} bytes)")
            return
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=content_type)
        print(f"  PUT {key} ({len(body):,} bytes)")

    def get_text(self, key: str) -> str | None:
        if self.out_dir:
            path = os.path.join(self.out_dir, key)
            return open(path, encoding="utf-8").read() if os.path.exists(path) else None
        if self.dry_run:
            return None
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=key)
            return obj["Body"].read().decode("utf-8")
        except self.client.exceptions.NoSuchKey:
            return None


# -------------------------------------------------------------------------- bookmap


def futures_basis(spot: float) -> dict[str, float] | None:
    """
    ES/MES minus SPX cash, measured live.

    Deliberately best-effort and deliberately downstream of the archive. The basis
    drifts with rates and dividends and resets each quarterly roll, so a stale or
    static offset smears every level (the SPX != ES trap in t-0353). If the quote
    source is unavailable we skip the Bookmap files entirely rather than publish
    levels at the wrong price.
    """
    try:
        import yfinance as yf

        quote = yf.Ticker("ES=F").fast_info
        es = float(quote["last_price"])
    except Exception as exc:  # noqa: BLE001 - any failure means "no basis today"
        print(f"  basis unavailable ({exc.__class__.__name__}: {exc}) — skipping Bookmap files")
        return None
    return {"ES": es - spot, "MES": es - spot}


def bookmap_csv(bm_symbol: str, levels: list[dict[str, Any]], basis: float) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(BM_HEADER)
    for lv in levels:
        fg, bg = LEVEL_COLORS.get(lv["type"], ("#FFFFFF", "#808080"))
        note = f"{lv['type']} {lv['strength']:.2f}"
        w.writerow([bm_symbol, f"{lv['level'] + basis:.2f}", note, fg, bg, "left", "TRUE"])
    return buf.getvalue().encode()


# ----------------------------------------------------------------------------- run


def session_date_for(now_utc: dt.datetime) -> dt.date:
    """The US trading date this snapshot belongs to (US/Eastern calendar day)."""
    try:
        from zoneinfo import ZoneInfo

        return now_utc.astimezone(ZoneInfo("America/New_York")).date()
    except Exception:  # noqa: BLE001
        return (now_utc - dt.timedelta(hours=5)).date()


def main() -> int:
    ap = argparse.ArgumentParser(description="Snapshot SPX gamma structure to R2 (t-0352).")
    ap.add_argument("--symbol", default="SPX", help="Cboe index symbol (default: SPX)")
    ap.add_argument("--bucket", default=os.environ.get("R2_BUCKET", "ante9-options"))
    ap.add_argument("--max-dte", type=int, default=7, help="keep expirations 0..N days out")
    ap.add_argument("--strike-pct", type=float, default=0.08, help="keep strikes within +-pct of spot")
    ap.add_argument("--top-n", type=int, default=5, help="how many GammaWall rows")
    ap.add_argument("--bookmap", action="store_true", help="also write basis-adjusted ES/MES cloud-notes CSVs")
    ap.add_argument("--dry-run", action="store_true", help="fetch and derive, write nothing")
    ap.add_argument("--out-dir", help="write to this local directory instead of R2 (offline test)")
    args = ap.parse_args()

    payload, fetched_at = fetch_chain(args.symbol)
    source_ts = payload.get("timestamp", "")
    session = session_date_for(fetched_at)
    stamp = fetched_at.strftime("%Y%m%dT%H%M%SZ")

    contracts, spot = parse_contracts(payload, args.max_dte, args.strike_pct, session)
    if not contracts:
        print(f"no contracts survived the filter (spot={spot}, session={session}) — nothing to archive")
        return 1

    print(f"{args.symbol} spot={spot:,.2f} session={session} snapshot={stamp} "
          f"source_ts={source_ts!r} contracts={len(contracts):,}")

    zero_dte = [c for c in contracts if c["_dte"] == 0]
    per_strike = strike_gex(contracts, spot, dte_filter=0 if zero_dte else None)
    expiration_tag = "0DTE" if zero_dte else f"{args.max_dte}DTE-ALL"
    if not zero_dte:
        print("  no 0DTE contracts in this snapshot (holiday/weekend?) — deriving across all kept expirations")

    levels = derive_levels(per_strike, args.top_n)
    for lv in levels:
        print(f"  {lv['type']:<14} {lv['level']:>10,.2f}  strength={lv['strength']:.3f}")

    r2 = R2(args.bucket, dry_run=args.dry_run, out_dir=args.out_dir)

    # 1. raw snapshot — the irreplaceable half. Written first, never overwritten.
    envelope = {
        "source": SOURCE_ID,
        "source_url": CBOE_URL.format(sym=args.symbol),
        "source_timestamp": source_ts,
        "fetched_at_utc": fetched_at.isoformat(),
        "session_date": session.isoformat(),
        "symbol": args.symbol,
        "spot": spot,
        "filter": {"max_dte": args.max_dte, "strike_pct": args.strike_pct},
        "contract_count": len(contracts),
        "contracts": contracts,
    }
    blob = io.BytesIO()
    with gzip.GzipFile(fileobj=blob, mode="wb", mtime=0) as gz:
        gz.write(json.dumps(envelope, separators=(",", ":")).encode())
    r2.put(
        f"raw/{args.symbol}/{session.isoformat()}/{stamp}.json.gz",
        blob.getvalue(),
        "application/gzip",
    )

    # 2. derived levels, appended as a time series for the session.
    key = f"levels/{args.symbol}/{session.isoformat()}.csv"
    existing = r2.get_text(key)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    if existing:
        buf.write(existing if existing.endswith("\n") else existing + "\n")
    else:
        w.writerow(ARCHIVE_HEADER)
    for lv in levels:
        w.writerow([
            args.symbol,
            f"{lv['level']:.2f}",
            lv["type"],
            f"{lv['strength']:.3f}",
            expiration_tag,
            fetched_at.strftime("%H:%M:%S"),
            fetched_at.isoformat(),
            source_ts,
            SOURCE_ID,
            f"{spot:.2f}",
            f"{lv['gex']:.2f}",
        ])
    r2.put(key, buf.getvalue().encode(), "text/csv")

    # 3. Bookmap-consumable current state. Best-effort, downstream, skippable.
    if args.bookmap:
        basis = futures_basis(spot)
        if basis:
            for feed, bm_symbol in (("ES", os.environ.get("BM_ES", "ESU6.CME@BMD")),
                                    ("MES", os.environ.get("BM_MES", "MESU6.CME@BMD"))):
                r2.put(
                    f"bookmap/{feed}.csv",
                    bookmap_csv(bm_symbol, levels, basis[feed]),
                    "text/csv",
                )
            print(f"  basis SPX->ES = {basis['ES']:+.2f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
