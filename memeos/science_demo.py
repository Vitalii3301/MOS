"""MOS discovers Kepler's third law from raw orbital data, transfers the theory to moon systems,
and derives the masses of the Sun, Jupiter and Saturn. Controls: shuffled and random data."""
import json, math, random, time
from science import accept_law, transfer, show, evaluate, fit

# reference orbital data (semi-major axis, sidereal period); mass is a deliberate distractor variable
PLANETS = [("Mercury", 0.387, 0.2408, 0.0553), ("Venus", 0.723, 0.6152, 0.815), ("Earth", 1.000, 1.0000, 1.0),
           ("Mars", 1.524, 1.8809, 0.107), ("Jupiter", 5.203, 11.862, 317.8), ("Saturn", 9.537, 29.457, 95.2),
           ("Uranus", 19.191, 84.011, 14.5), ("Neptune", 30.069, 164.79, 17.1)]
JUPITER = [("Io", 421700, 1.769138), ("Europa", 671034, 3.551181), ("Ganymede", 1070412, 7.154553), ("Callisto", 1882709, 16.689018)]
SATURN = [("Mimas", 185539, 0.942422), ("Enceladus", 237948, 1.370218), ("Tethys", 294619, 1.887802), ("Dione", 377396, 2.736915),
          ("Rhea", 527108, 4.518212), ("Titan", 1221870, 15.945421), ("Iapetus", 3560820, 79.3215)]
G = 6.674e-11; AU = 1.495978707e11; YEAR = 3.15576e7; DAY = 86400.0
KNOWN = {"Sun": 1.989e30, "Jupiter": 1.898e27, "Saturn": 5.683e26}

def central_mass(law, rows_si):
    """T = k * f(a) through the origin in SI units -> M = 4*pi^2 / (G k^2) if f(a) = a^1.5."""
    fx = [evaluate(law["expr"], {"a": a}) for a, T in rows_si]; ys = [T for a, T in rows_si]
    k = sum(f * y for f, y in zip(fx, ys)) / sum(f * f for f in fx)
    return 4 * math.pi ** 2 / (G * k * k)

def run():
    t0 = time.time(); rng = random.Random(1); out = {}
    rows = [{"name": n, "a": a, "T": T, "mass": m} for n, a, T, m in PLANETS]
    test_names = {"Earth", "Saturn", "Neptune"}
    train = [r for r in rows if r["name"] not in test_names]; test = [r for r in rows if r["name"] in test_names]
    res = accept_law(train, test, "T", ["a", "mass"], max_size=6)
    L = res["law"]; out["planets"] = {"accepted": res["accepted"], "law": f"T = {L['a']:.4g} + {L['b']:.4g}·{show(L['expr'])}",
        "holdout_planets": sorted(test_names), "holdout_max_rel_err": round(L["holdout_max_rel_err"], 5),
        "uses_distractor_mass": "mass" in show(L["expr"]),
        "runners_up": [f"{show(h['expr'])} (holdout err {h['holdout_max_rel_err']:.3g})" for h in res["runners_up"]]}
    # theory transfer: same SHAPE, constants refit on 2 moons, must predict the others
    for sysname, moons in (("Jupiter", JUPITER), ("Saturn", SATURN)):
        mr = [{"a": a, "T": T} for n, a, T in moons]
        tr = transfer(L, mr[:2], "T", mr[2:])
        out["transfer_" + sysname] = {"survives": tr["survives"], "fit_on": [n for n, *_ in moons[:2]],
                                      "predicted": [n for n, *_ in moons[2:]], "holdout_max_rel_err": round(tr["holdout_max_rel_err"], 5)}
    # derived quantities nobody gave the system: masses of central bodies
    out["derived_masses_kg"] = {
        "Sun": central_mass(L, [(a * AU, T * YEAR) for _, a, T, _ in PLANETS]),
        "Jupiter": central_mass(L, [(a * 1000, T * DAY) for _, a, T in JUPITER]),
        "Saturn": central_mass(L, [(a * 1000, T * DAY) for _, a, T in SATURN])}
    out["mass_error_vs_reference"] = {k: round(abs(v - KNOWN[k]) / KNOWN[k], 4) for k, v in out["derived_masses_kg"].items()}
    # controls: no structure -> must say "no law"
    shuffled = [dict(r, T=t) for r, t in zip(rows, rng.sample([r["T"] for r in rows], len(rows)))]
    sh = accept_law([r for r in shuffled if r["name"] not in test_names], [r for r in shuffled if r["name"] in test_names], "T", ["a", "mass"], max_size=6)
    rnd = [dict(r, T=rng.uniform(0.2, 165)) for r in rows]
    rd = accept_law([r for r in rnd if r["name"] not in test_names], [r for r in rnd if r["name"] in test_names], "T", ["a", "mass"], max_size=6)
    out["control_shuffled"] = {"accepted": sh["accepted"], "best_overfit": show(sh["law"]["expr"]), "holdout_max_rel_err": round(sh["law"]["holdout_max_rel_err"], 3)}
    out["control_random"] = {"accepted": rd["accepted"], "best_overfit": show(rd["law"]["expr"]), "holdout_max_rel_err": round(rd["law"]["holdout_max_rel_err"], 3)}
    out["seconds"] = round(time.time() - t0, 1)
    return out

if __name__ == "__main__":
    r = run(); json.dump(r, open("evidence/science_kepler.json", "w"), indent=1, ensure_ascii=False)
    print(json.dumps(r, indent=1, ensure_ascii=False))
