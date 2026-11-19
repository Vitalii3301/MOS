"""Benchmark on real public series, forecast origins strictly after Chronos-2's release (2025-11-01 .. latest)."""
import csv, json, sys, time, os
sys.path.insert(0, "/home/claude/memeos")
import forecast as F, external_gene
TS = sys.argv[1]; GENES = sys.argv[2]
def rd(path, dcol, vcol, filt=None, skip_bad=True):
    out = []
    with open(os.path.join(TS, path)) as f:
        r = csv.reader(f); next(r)
        for row in r:
            if filt and not filt(row): continue
            try: v = float(row[vcol])
            except (ValueError, IndexError): continue
            if v <= -90: continue
            out.append((row[dcol][:10], v))
    return out
S = {
 "brent_daily": (rd("oil-prices/data/brent-daily.csv", 0, 1), 5, 1, 5),
 "wti_daily": (rd("oil-prices/data/wti-daily.csv", 0, 1), 5, 1, 5),
 "natgas_daily": (rd("natural-gas/data/daily.csv", 0, 1), 5, 1, 5),
 "brent_weekly": (rd("oil-prices/data/brent-weekly.csv", 0, 1), 4, 1, 1),
 "wti_weekly": (rd("oil-prices/data/wti-weekly.csv", 0, 1), 4, 1, 1),
 "co2_mauna_loa_monthly": (rd("co2-ppm/data/co2-mm-mlo.csv", 0, 2), 3, 12, 1),
 "us_house_prices_nsa_monthly": (rd("house-prices-us/data/national-month.csv", 0, 2), 3, 12, 1),
 "us_cpi_monthly": (rd("cpi-us/data/cpiai.csv", 0, 1), 3, 12, 1),
 "global_temp_anomaly_monthly": (rd("global-temp/data/monthly.csv", 1, 2, lambda r: r[0] == "GCAG"), 3, 12, 1),
 "gold_monthly": (rd("gold-prices/data/monthly.csv", 0, 1), 3, 1, 1),
 "natgas_monthly": (rd("natural-gas/data/monthly.csv", 0, 1), 3, 12, 1),
 "us10y_yield_monthly": (rd("bond-yields-us-10y/data/monthly.csv", 0, 1), 3, 1, 1),
 "sp500_monthly": (rd("s-and-p-500/data/data.csv", 0, 1), 3, 1, 1),
}
for cur in ("United Kingdom", "Japan", "Switzerland", "Canada", "Australia", "Euro", "India", "Brazil", "Mexico", "China"):
    S["fx_" + cur.lower().replace(" ", "_") + "_daily"] = (rd("exchange-rates/data/daily.csv", 0, 2, lambda r, c=cur: r[1] == c), 5, 1, 5)
gene = [g for g in external_gene.load_genes(GENES) if g.role == "forecaster"][0]; ext = F.Chronos2Gene(gene)
CUT = "2025-11-01"; res = {}; t0 = time.time()
for name, (rows, h, m, step) in S.items():
    rows = sorted(rows); y = [v for _, v in rows][-2600:]; dates = [d for d, _ in rows][-2600:]
    first = next(i for i, d in enumerate(dates) if d >= CUT)
    origins = list(range(first, len(y) - h + 1, step))
    agg = {k: [] for k in list(F.SIMPLE) + ["chronos2_small", "mos_selected"]}; cov = {"mos_selected": [], "chronos2_small": []}; chosen = []
    for t in origins:
        hist, fut = y[:t], y[t:t + h]; sc = F.scale(hist, m)
        for k, f in F.SIMPLE.items():
            p = f(hist, h, m); agg[k].append(sum(abs(a - b) for a, b in zip(p, fut)) / h / sc)
        q = ext.quantiles(hist, h); p = q[0.5]
        agg["chronos2_small"].append(sum(abs(a - b) for a, b in zip(p, fut)) / h / sc)
        cov["chronos2_small"] += [lo <= v <= hi for lo, hi, v in zip(q[0.1], q[0.9], fut)]
        d = F.forecast(hist, h, m, external=ext, n_val=12, step=step)
        agg["mos_selected"].append(sum(abs(a - b) for a, b in zip(d["forecast"], fut)) / h / sc); chosen.append(d["chosen"])
        cov["mos_selected"] += [lo <= v <= hi for lo, hi, v in zip(d["interval80"][0], d["interval80"][1], fut)]
    res[name] = {"h": h, "m": m, "origins": len(origins), "first_origin": dates[origins[0]], "last_origin": dates[origins[-1]],
                 "mase": {k: round(sum(v) / len(v), 4) for k, v in agg.items()},
                 "coverage80": {k: round(sum(v) / len(v), 3) for k, v in cov.items()},
                 "chosen": {c: chosen.count(c) for c in set(chosen)}}
    print(name, json.dumps(res[name]["mase"]), res[name]["coverage80"], res[name]["chosen"], round(time.time() - t0), flush=True)
json.dump(res, open(sys.argv[3], "w"), indent=1)
