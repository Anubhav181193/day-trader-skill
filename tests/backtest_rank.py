#!/usr/bin/env python3
"""Check the daily scan's ranking on past data. Not part of the test suite (it needs the network).

    python3 tests/backtest_rank.py fetch data.json.gz              # 2 years of daily bars, 500 most traded US names
    python3 tests/backtest_rank.py fetch data.json.gz --universe trader/universe-ca.txt --top 200
    python3 tests/backtest_rank.py run data.json.gz                # the last 250 sessions

Each past session t: the scanner's own setup rules on the 64 closes to t (what the 3-month spark
gives), the $5 and 1,000,000-share floors, then each hit's relative volume (session t over the 20
before) and move ratio, exactly as scan.py computes them. What came next:
  range     the next session's high - low over its open, and over the stock's own 20-day average
  |o-c|     the next session's open to close, either way (what a day trade can catch)
  excess    the next session's open to close, and the 5 sessions' close to close, minus the
            day's average over every name, so a rising or falling market flatters no one
The universe is today's most traded names, so it leans toward stocks that did well: read the
direction columns with that in mind. The move columns do not depend on it.
"""
import argparse
import gzip
import json
import math
import os
import statistics as st
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trader-scanner"))
import scan  # noqa: E402


def cmd_fetch(a):
    syms = scan.symbols_from_file(a.universe)
    data, _ = scan.fetch_spark(syms, "5d", "1d", scan.PAUSE)
    dv = []
    for s, ser in data.items():
        m = ser.get("meta") or {}
        p, v = m.get("regularMarketPrice"), m.get("regularMarketVolume")
        if p and v and p >= 5 and str(m.get("instrumentType", "")).upper() not in scan.NOT_A_STOCK:
            dv.append((p * v, s))
    pick = [s for _, s in sorted(dv, reverse=True)[:a.top]]
    charts = {}
    for i, s in enumerate(pick):
        time.sleep(scan.PAUSE)
        try:
            charts[s] = json.loads(scan.yahoo_get(scan.chart_path(s, "2y", "1d")))
        except Exception as e:  # one bad chart is not a reason to stop
            print("skipped %s: %s" % (s, e), file=sys.stderr)
        if (i + 1) % 100 == 0:
            print("%d of %d charts" % (i + 1, len(pick)), file=sys.stderr)
    with gzip.open(a.out, "wt") as fh:
        json.dump(charts, fh)
    print("Saved %d daily charts to %s" % (len(charts), a.out))


def load(path):
    rows = {}
    for sym, doc in json.load(gzip.open(path, "rt")).items():
        try:
            s = scan.parse_chart(doc, sym)
        except Exception:
            continue
        r = {}
        for i, ts in enumerate(s["t"]):
            vals = [s[k][i] if s.get(k) else None for k in ("o", "h", "l", "c", "v")]
            if None not in vals:
                r[scan.ts_to_eastern(ts).date()] = vals
        rows[sym] = r
    return rows


def band(xs):
    return 1.96 * st.stdev(xs) / math.sqrt(len(xs)) if len(xs) > 1 else float("nan")


def cmd_run(a):
    rows = load(a.data)
    dates = sorted({d for r in rows.values() for d in r})
    dates = [d for d in dates if sum(1 for r in rows.values() if d in r) > 0.8 * len(rows)]
    col = {k: {s: [rows[s][d]["ohlcv".index(k)] if d in rows[s] else None for d in dates] for s in rows}
           for k in "ohlcv"}
    O, H, L, C, V = (col[k] for k in "ohlcv")
    start = max(64 + 21, len(dates) - a.days - 5)
    rec = []
    for t in range(start, len(dates) - 5):
        names = [s for s in rows if None not in C[s][t - 63:t + 6] and O[s][t + 1] and None not in V[s][t - 20:t + 1]]
        if not names:
            continue
        base_oc = st.mean(C[s][t + 1] / O[s][t + 1] - 1 for s in names)
        base_5 = st.mean(C[s][t + 5] / C[s][t] - 1 for s in names)
        for s in names:
            w = C[s][t - 63:t + 1]
            if w[-1] < 5 or V[s][t] < 1e6:
                continue
            tags, _ = scan.find_setups(w)
            if not tags:
                continue
            avg_v = st.mean(V[s][t - 20:t])
            rng = (H[s][t + 1] - L[s][t + 1]) / O[s][t + 1]
            own = st.mean((H[s][j] - L[s][j]) / O[s][j] for j in range(t - 19, t + 1))
            oc = C[s][t + 1] / O[s][t + 1] - 1
            rec.append({"t": t, "s": s, "rvol": V[s][t] / avg_v if avg_v else 0, "mr": scan.move_ratio(w) or 0,
                        "dollars": w[-1] * V[s][t], "range": rng, "range_x": rng / own if own else 1,
                        "abs_oc": abs(oc), "exc_oc": oc - base_oc, "exc_5": C[s][t + 5] / C[s][t] - 1 - base_5})
    by_day = {}
    for r in rec:
        by_day.setdefault(r["t"], []).append(r)
    pick = {"all hits": [], "top 10, as the scanner ranks": [], "top 10 by relative volume (all fetched)": [],
            "top 10 most dollars traded (--sort traded)": []}
    for rs in by_day.values():
        pick["all hits"] += rs
        top50 = sorted(rs, key=lambda r: -r["mr"])[:scan.RANK_CONFIRM]
        pick["top 10, as the scanner ranks"] += sorted(top50, key=lambda r: -r["rvol"])[:scan.RANK_TOP]
        pick["top 10 by relative volume (all fetched)"] += sorted(rs, key=lambda r: -r["rvol"])[:scan.RANK_TOP]
        pick["top 10 most dollars traded (--sort traded)"] += sorted(rs, key=lambda r: -r["dollars"])[:scan.RANK_TOP]
    d0, d1 = dates[start], dates[len(dates) - 6]
    print("%d names, %d sessions with hits (%s to %s), %d hits, %.0f a session"
          % (len(rows), len(by_day), d0, d1, len(rec), len(rec) / max(1, len(by_day))))
    print("\nThe next session, by how the hits were picked:")
    print("  %-44s %6s  %6s  %8s  %6s  %-17s %-17s %s" % ("", "n", "range", "x own", "|o-c|", "o-c excess",
                                                          "5-day excess", "beat 5d"))
    for name, rs in pick.items():
        e1, e5 = [r["exc_oc"] for r in rs], [r["exc_5"] for r in rs]
        print("  %-44s %6d  %5.2f%%  %7.2fx  %5.2f%%  %+.2f%% +-%.2f   %+.2f%% +-%.2f   %4.1f%%" % (
            name, len(rs), 100 * st.mean(r["range"] for r in rs), st.mean(r["range_x"] for r in rs),
            100 * st.mean(r["abs_oc"] for r in rs), 100 * st.mean(e1), 100 * band(e1), 100 * st.mean(e5),
            100 * band(e5), 100.0 * sum(1 for x in e5 if x > 0) / len(e5)))
    print("\nBy relative volume on the signal day:")
    for lo, hi in ((0, .7), (.7, 1), (1, 1.5), (1.5, 2), (2, 3), (3, 1e9)):
        rs = [r for r in rec if lo <= r["rvol"] < hi]
        if len(rs) > 1:
            print("  %-12s n=%6d  next range %.2fx its own average   |o-c| %.2f%%   5-day excess %+.2f%% +-%.2f" % (
                ("%.1f-%.1fx" % (lo, hi)) if hi < 1e9 else "over %.0fx" % lo, len(rs),
                st.mean(r["range_x"] for r in rs), 100 * st.mean(r["abs_oc"] for r in rs),
                100 * st.mean(r["exc_5"] for r in rs), 100 * band([r["exc_5"] for r in rs])))
    print("\n+- is a 95 % band. A bigger range is room to trade; it is not a direction.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("fetch")
    p.add_argument("out")
    p.add_argument("--universe", default=scan.DEFAULT_UNIVERSE)
    p.add_argument("--top", type=int, default=500)
    p = sub.add_parser("run")
    p.add_argument("data")
    p.add_argument("--days", type=int, default=250)
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        return cmd_fetch(a)
    if a.cmd == "run":
        return cmd_run(a)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
