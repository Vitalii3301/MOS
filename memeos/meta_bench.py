"""Powered test of meta-evolution (plan item: "meta-evolution on a larger task set, with enough statistical power").

Question: does the search configuration that MOS evolved for itself (evidence/metaevolution.json, selected on six
META_TRAIN tasks) solve more NEW tasks than the default configuration?

Pre-registered before the run (this file is committed with its hash before results exist):
  tasks      40 fresh realistic text/data tasks below, none used by the meta-search; inputs are names, e-mails, CSV
             rows, tags, product titles, sentences, number lists. Each: 6 train, 4 validation, 6 holdout examples,
             generated deterministically. Ground truth = a plain Python reference function.
  runs       each task x seeds 0..4 x {default, evolved}; same learn() budget as the meta-search (generations=60,
             restarts=2, max_candidates=4, enumerate_first=False, compose/formula shortcuts OFF so the search engine
             itself is measured).
  primary    "solved" = learn() accepted a meme through the validation gate AND the meme is correct on all 6 holdout
             examples. Paired by (task, seed). Two-sided exact McNemar test on discordant pairs, alpha = 0.05.
  secondary  evaluations used (paired, sign test on the pairs where both solved); per-task solve counts.
"""
import json, math, random, sys, time
from memeos import MemeOS, DEFAULT_CONFIG, evaluate

FIRST = ["Анна", "Иван", "Olga", "Peter", "Maria", "Sergey", "Lena", "John", "Dmitry", "Emma", "Pavel", "Nina", "Oleg", "Sofia", "Max", "Irina"]
LAST = ["Petrova", "Smirnov", "Kowalski", "Novak", "Ivanova", "Brown", "Sokolov", "Muller", "Orlova", "Garcia", "Kuznetsov", "Lee"]
DOM = ["gmail.com", "yandex.ru", "mail.ru", "company.org", "shop.ru", "outlook.com", "inbox.eu"]
WORDS = ["fast", "delivery", "great", "price", "quality", "seller", "box", "cable", "phone", "charger", "red", "blue", "small", "large", "new", "used",
         "кофе", "чай", "доставка", "товар", "цена", "отзыв", "магазин", "скидка"]
TAGS = ["python", "ml", "data", "web", "api", "rust", "go", "sql", "devops", "ui", "mobile", "cloud"]
BRANDS = ["Bosch", "Sony", "Philips", "Xiaomi", "Apple", "Samsung", "Dyson", "Lenovo"]

def _rng(task, i): return random.Random(f"{task}:{i}")
def name(r): return f"{r.choice(FIRST)} {r.choice(LAST)}"
def email(r): return f"{r.choice(FIRST).lower()}.{r.choice(LAST).lower()}@{r.choice(DOM)}"
def sentence(r, a=2, b=6): return " ".join(r.choice(WORDS) for _ in range(r.randint(a, b)))
def nums(r, a=2, b=6): return [r.randint(1, 99) for _ in range(r.randint(a, b))]
def csvnums(r): return ",".join(str(x) for x in nums(r))
def tags(r): return ";".join(r.sample(TAGS, r.randint(2, 5)))
def product(r): return f"{r.choice(BRANDS)}  {r.choice(WORDS).title()} {r.randint(10, 999)}"

# name: (input generator, reference function)
BENCH = {
    "email_domain":        (email, lambda s: s.split("@")[1]),
    "email_user":          (email, lambda s: s.split("@")[0]),
    "email_upper_domain":  (email, lambda s: s.split("@")[1].upper()),
    "name_last":           (name, lambda s: s.split()[-1]),
    "name_first_lower":    (name, lambda s: s.split()[0].lower()),
    "name_upper":          (name, lambda s: s.upper()),
    "name_swap":           (name, lambda s: " ".join(s.split()[::-1])),
    "name_initial_last":   (name, lambda s: s.split()[0][0] + ". " + s.split()[1]),
    "name_underscore":     (name, lambda s: "_".join(s.lower().split())),
    "csv_count":           (csvnums, lambda s: len(s.split(","))),
    "csv_first":           (csvnums, lambda s: s.split(",")[0]),
    "csv_last":            (csvnums, lambda s: s.split(",")[-1]),
    "csv_sorted_desc":     (csvnums, lambda s: ",".join(sorted(s.split(","), reverse=True))),
    "csv_to_spaces":       (csvnums, lambda s: s.replace(",", " ")),
    "csv_reverse":         (csvnums, lambda s: ",".join(s.split(",")[::-1])),
    "list_sum":            (nums, lambda x: sum(x)),
    "list_len":            (nums, lambda x: len(x)),
    "list_first":          (nums, lambda x: x[0]),
    "list_last":           (nums, lambda x: x[-1]),
    "list_sorted":         (nums, lambda x: sorted(x)),
    "list_reversed":       (nums, lambda x: x[::-1]),
    "list_unique_sorted":  (lambda r: [r.choice([3, 5, 7, 9, 11]) for _ in range(r.randint(3, 7))], lambda x: sorted(set(x))),
    "list_top2":           (nums, lambda x: sorted(x)[-2:][::-1] if len(x) >= 2 else x),
    "tags_count":          (tags, lambda s: len(s.split(";"))),
    "tags_sorted":         (tags, lambda s: ";".join(sorted(s.split(";")))),
    "tags_upper_comma":    (tags, lambda s: s.upper().replace(";", ",")),
    "tags_first":          (tags, lambda s: s.split(";")[0]),
    "tags_hashtags":       (tags, lambda s: " ".join("#" + t for t in s.split(";"))),
    "sentence_words":      (sentence, lambda s: len(s.split())),
    "sentence_upper":      (sentence, lambda s: s.upper()),
    "sentence_first_word": (sentence, lambda s: s.split()[0]),
    "sentence_last_word":  (sentence, lambda s: s.split()[-1]),
    "sentence_kebab":      (sentence, lambda s: "-".join(s.split())),
    "sentence_title":      (sentence, lambda s: s.title()),
    "sentence_sorted":     (sentence, lambda s: " ".join(sorted(s.split()))),
    "product_clean":       (product, lambda s: " ".join(s.split())),
    "product_brand":       (product, lambda s: s.split()[0]),
    "product_lower_slug":  (product, lambda s: "-".join(s.lower().split())),
    "product_model_num":   (product, lambda s: "".join(c for c in s if c.isdigit())),
    "padded_trim_lower":   (lambda r: "  " * r.randint(0, 2) + r.choice(WORDS).upper() + " " * r.randint(1, 3), lambda s: s.strip().lower()),
}

def build(task):
    gen, ref = BENCH[task]; ex = []; seen = set(); i = 0
    while len(ex) < 16 and i < 400:
        x = gen(_rng(task, i)); i += 1; key = json.dumps(x, ensure_ascii=False)
        if key in seen: continue
        seen.add(key); ex.append({"input": x, "output": ref(x)})
    return ex[:6], ex[6:10], ex[10:16]

def run_one(task, seed, cfg):
    tr, va, ho = build(task); k = MemeOS(seed=seed)
    r = k.learn(k.contract(task, tr, va, ho), enumerate_first=False, compose_first=False, formula_first=False, rng=random.Random(seed),
                generations=60, restarts=2, max_candidates=4, config=cfg)
    ok = False
    if r["ok"]:
        _, good, n = evaluate(k.card(r["meme"])["genome"], ho, k.resolve); ok = good == n
    return {"task": task, "seed": seed, "gate": bool(r["ok"]), "solved": ok, "evaluations": r["evaluations"]}

def mcnemar_exact(b, c):
    n = b + c
    if n == 0: return 1.0
    k = min(b, c); p = sum(math.comb(n, i) for i in range(0, k + 1)) / 2 ** n
    return min(1.0, 2 * p)

def _worker(args):
    task, seed, label, cfg = args
    t = time.time(); r = run_one(task, seed, cfg); r["config"] = label; r["seconds"] = round(time.time() - t, 2); return r

if __name__ == "__main__":
    import multiprocessing as mp
    evolved = json.load(open("evidence/metaevolution.json"))["evolved"]
    jobs = [(t, s, lab, cfg) for t in BENCH for s in range(5) for lab, cfg in (("default", DEFAULT_CONFIG), ("evolved", evolved))]
    out = sys.argv[1] if len(sys.argv) > 1 else "meta_bench_runs.jsonl"
    done = set()
    try:
        for l in open(out): x = json.loads(l); done.add((x["task"], x["seed"], x["config"]))
    except OSError: pass
    todo = [j for j in jobs if (j[0], j[1], j[2]) not in done]
    with mp.Pool(int(sys.argv[2]) if len(sys.argv) > 2 else 2) as pool, open(out, "a") as f:
        for r in pool.imap_unordered(_worker, todo):
            f.write(json.dumps(r) + "\n"); f.flush()
