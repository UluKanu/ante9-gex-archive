# ante9-gex-archive

Snapshots SPX gamma structure every 5 minutes through the session and archives it to
Cloudflare R2. Sibling of [`UluKanu/pini-bot`](https://github.com/UluKanu/pini-bot) —
same shape, same hosting, same $0 running cost.

Task: **t-0352**. Design context: `Ante 9 — Options Intelligence Engine.md` in the vault.

## Why this exists, and why it is urgent

Historical vendors sell **end-of-day** option chains cheaply and deeply. **Intraday**
chain snapshots are rare and expensive, and nobody sells you last Tuesday's 14:35
gamma picture after the fact.

So the one dataset that cannot be bought back later is the one a cron collects for
free starting today. Every session this does not run is permanently gone. That is why
it ships before the study that would justify it (t-0353), not after — the study can be
run on any Saturday; the archive cannot be run retroactively.

In ~3 months this is ~60 sessions of intraday-accurate gamma structure, time-aligned
with Matt's own Bookmap recordings. That is the only route to "Study B" (the true
intraday 0DTE build), which cannot be backtested on purchased history at any price.

## Data source

**Cboe delayed quotes** — free, public, no key, ~15 minutes delayed:

```
https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json
```

Returns ~28,000 SPX + SPXW contracts with per-contract `gamma`, `delta`,
`open_interest`, `volume`, and the cash index spot. That is everything the gamma math
needs, which means **the archive can start today without the ~$150/mo Unusual Whales
subscription**. This is the cheap-first rule from t-0353: a rough archive running today
beats a perfect archive starting in November.

**The 15-minute delay does not damage the archive** — every row records the actual
fetch time and the source's own timestamp, so a delayed snapshot is still a correctly
labelled point in a time series. It would matter for live trading; it does not matter
for building history. If a real-time source is bought later, add it as a *second*
`Source` value. **Never pool sources into one series** — same discipline as the
backfill `Source` column in t-0339.

## What lands in R2

```
raw/SPX/2026-09-02/20260902T143502Z.json.gz     the asset — never overwritten
levels/SPX/2026-09-02.csv                        derived levels, appended all day
bookmap/ES.csv                                   current state, Bookmap-consumable
bookmap/MES.csv
```

### 1. `raw/` — the irreplaceable half

The filtered chain exactly as the source returned it, wrapped in a provenance envelope
(source id, source URL, source timestamp, our fetch time, session date, spot, filter
settings). Filtered to **0–7 DTE within ±8% of spot**, which is where all the gamma
that matters lives — about 2,000 contracts, ~120 KB gzipped per snapshot.

At 108 snapshots/day that is ~13 MB/day, ~280 MB/month. R2's free tier is 10 GB, so
this runs about three years before storage is a conversation.

Written **first**, before anything derived. If the derivation logic turns out to be
wrong in November, the raw chains are still there to recompute from. That is the whole
point of keeping them.

### 2. `levels/` — the derived time series

One block of rows per snapshot, appended to a per-session CSV:

```
Symbol,Level,Type,Strength,Expiration,Updated,SnapshotUTC,SourceTimestamp,Source,Spot,GexNotional
SPX,6910.00,GammaWall,0.940,0DTE,14:35:02,2026-09-02T14:35:02+00:00,2026-09-02 14:20:11,cboe-delayed-quotes,6903.44,2318400012.55
```

The first six columns are the settled Ante 9 level format. The rest is the provenance
that makes it worth anything in three months.

`Updated` and `SnapshotUTC` are **our fetch clock**, not the schedule — see *Cadence*
below. Levels stay in **SPX cash points** here. No basis adjustment ever touches the
archive.

Types emitted: `GammaFlip`, `CallWall`, `PutWall`, `PositiveGamma`, `NegativeGamma`,
and the top *N* `GammaWall` strikes by absolute net GEX.

### 3. `bookmap/` — the live feed, as a side effect

Current state only, in the same cloud-notes CSV format pini-bot already serves, with
levels converted from SPX cash into ES/MES points. Point Ante 9 at these URLs and the
archive doubles as a working levels feed today.

This half is **best-effort and downstream on purpose.** The SPX↔ES basis drifts with
rates and dividends and resets each quarterly roll, so it is measured live per run
rather than hardcoded. If the quote source is unavailable, the Bookmap files are
skipped and the archive is unaffected. A bad basis can never contaminate history.

⚠️ Contract roll: `BM_ES` / `BM_MES` repo variables carry the Bookmap symbols
(`ESU6.CME@BMD` etc.) and need the same quarterly edit as pini-bot. **Next roll ~Sept
18, 2026** → `ESZ6` / `MESZ6`.

## The gamma math, stated honestly

```
gex_per_strike = gamma × open_interest × 100 × spot² × 0.01     ($ per 1% move)
```

Two things in here are **assumptions, not measurements**, and both are the kind of
thing that reads as fact once it is drawn on a chart:

1. **Dealer positioning.** Calls count positive, puts count negative — the standard
   "dealers are long calls, short puts" convention. Nobody publishes actual dealer
   inventory. This is a convention that is often roughly right and sometimes badly
   wrong.

2. **`GammaFlip` is a proxy.** It is the strike where *cumulative* net GEX crosses
   zero, walking strikes upward, linearly interpolated between the two bracketing
   strikes. A true zero-gamma level requires re-solving every contract's gamma at each
   hypothetical spot. The cheap version is what most public GEX dashboards show. When
   cumulative GEX does not cross zero inside the strike band — which happens in a
   deeply negative-gamma tape — **no `GammaFlip` row is emitted at all** rather than a
   fabricated one.

`Strength` is `|gex| / peak` within the snapshot, on one shared scale across all level
types so a `CallWall` at 0.87 and a `GammaWall` at 0.29 are actually comparable.
`GammaFlip` is fixed at 1.0 because it is a location, not a magnitude.

This is the t-0314 standing check applied to a new indicator: say what is measured,
say what is assumed, and emit nothing rather than something confidently wrong.

## Cadence and clock discipline

`*/5 13-21 * * 1-5` UTC — every 5 minutes, covering RTH under both EDT (13:30–20:00
UTC) and EST (14:30–21:00 UTC) with no seasonal edit.

**GitHub's scheduler is best-effort and drifts 5–15 minutes under load.** That is fine
for an archive and fatal for anything sub-minute. It is also exactly why every row
carries the **actual fetch time**, never the scheduled time. An SI event at 14:35:12 in
a Bookmap recording gets matched to the snapshot whose `SnapshotUTC` actually says
14:35:02 — not to the run that was *supposed* to fire at 14:35:00 and fired at 14:41.

`concurrency: gex-archive` prevents a slow run and its successor from appending to the
same daily CSV at once.

## Setup

**1. R2 bucket.** Cloudflare dashboard → R2 → create bucket `ante9-options`. Enable
public access via the r2.dev subdomain only if you want the `bookmap/` CSVs reachable
by Bookmap (you do). Note that this makes `raw/` and `levels/` publicly readable too —
acceptable, since it is all derived from a free public endpoint.

**2. R2 API token.** R2 → Manage API Tokens → create with **Object Read & Write**.
The secret is shown once.

**3. Repo secrets** — Settings → Secrets and variables → Actions:

| Secret | Value |
|---|---|
| `R2_ACCOUNT_ID` | Cloudflare account id (also in any bucket's S3 API endpoint) |
| `R2_ACCESS_KEY_ID` | from the API token |
| `R2_SECRET_ACCESS_KEY` | from the API token |
| `R2_BUCKET` | `ante9-options` |

**4. Repo visibility: public.** Same reason as pini-bot — scheduled Actions at this
cadence are only free on public repos. The code has no secrets in it and the data comes
from a public endpoint.

**5. First run.** Actions → *Archive SPX gamma structure* → **Run workflow**. Do this
during RTH so there are 0DTE contracts to derive from.

## Running it locally

```bash
pip install -r requirements.txt

python fetch_gex.py --dry-run              # fetch + derive, write nothing
python fetch_gex.py --out-dir ./out        # write the same keys to a local tree
python fetch_gex.py --bookmap              # real run, needs the R2_* env vars
```

Useful flags: `--max-dte` (default 7), `--strike-pct` (default 0.08), `--top-n`
(default 5 GammaWall rows), `--symbol` (default SPX).

Outside market hours there are no 0DTE contracts, so the script says so and derives
across every expiration it kept. That is a smoke test, not data — weekend rows are
tagged `7DTE-ALL` in the `Expiration` column rather than `0DTE`, so they are trivially
filterable out later.

## Known limits

- **5-minute floor.** The design doc wants 30–60s recomputation. GitHub Actions cannot
  go below 5 minutes and drifts. If sub-minute matters later, this moves to a cheap
  always-on host — the script does not change, only the scheduler.
- **Open interest updates once daily.** Intraday variation in this archive comes from
  spot moving through the strike ladder (which reshapes gamma) and from volume, not
  from OI. True intraday position change needs a paid real-time source.
- **SPX only.** `--symbol` accepts other Cboe index symbols but nothing else has been
  looked at.
- **No holiday calendar.** The cron fires on market holidays and archives a stale
  chain. Cheap to filter out later by the `0DTE`/`7DTE-ALL` tag; not worth a dependency
  now.
