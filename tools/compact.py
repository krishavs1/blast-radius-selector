"""Fold the raw per-test traces into one compact map file (maps.bin)."""
import sys, pickle, zlib, json, glob, os
rawdir, tests_json, out = sys.argv[1:4]
r = {}
for f in sorted(glob.glob(rawdir + "/s*.pickle")):
    r.update(pickle.load(open(f, "rb")))
tests = [t["id"] for t in json.load(open(tests_json))["tests"]]
idx = {t: i for i, t in enumerate(tests)}
files = [t.split("::")[0] for t in tests]

lines = {}    # file -> line -> bitset
classes = {}  # "mod:Qual" -> bitset
opens = {}    # path -> bitset
imports = {}  # module-ish name -> bitset
implines = {} # file -> set(lines) executed while importing

def add(d, k, bit):
    d[k] = d.get(k, 0) | bit

for ctx in ("import:sqlglot", "import:all"):
    for f, l in r[ctx]["lines"]:
        implines.setdefault(f, set()).add(l)

for i, t in enumerate(tests):
    bit = 1 << i
    for src in (r[t], r.get("fileimport:" + files[i])):
        if src is None:
            continue
        isimp = src is not r[t]
        for f, l in src["lines"]:
            if isimp and f.startswith("sqlglot/"):
                implines.setdefault(f, set()).add(l)
                continue
            add(lines.setdefault(f, {}), l, bit)
        for c in src["classes"]:
            add(classes, c, bit)
        for o in src["opens"]:
            add(opens, o, bit)
        for m in src["imports"]:
            add(imports, m, bit)

# CPU seconds each test takes in the selector's own runner (measured on the
# pinned commit); used only to decide what is worth running
try:
    _cpu = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cpu.json")))
except Exception:
    _cpu = {}
_rec = {t["id"]: t["seconds"] for t in json.load(open(tests_json))["tests"]}
cpu = [_cpu.get(t) for t in tests]
for _i, _t in enumerate(tests):
    # the first test of a class was charged the class set-up: use its recorded time
    if cpu[_i] is not None and cpu[_i] > 10 * _rec[_t] + 0.3:
        cpu[_i] = _rec[_t]
data = {
    "tests": tests,
    "lines": lines,
    "classes": classes,
    "opens": opens,
    "imports": imports,
    "implines": {f: sorted(s) for f, s in implines.items()},
    "cpu": cpu,
    "deps": json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "deps.json"))),
}
blob = zlib.compress(pickle.dumps(data, protocol=5), 9)
open(out, "wb").write(blob)
print(len(blob), "bytes;", sum(len(v) for v in lines.values()), "lines;", len(classes), "classes;", len(opens), "opens")

# ---- per-unit coverage for tests that hand work to a process pool
if len(sys.argv) > 4:
    recs = {t["id"]: t["seconds"] for t in json.load(open(tests_json))["tests"]}
    units = {}
    for i, t in enumerate(tests):
        us = r[t].get("units") or []
        if len(us) < 2 or recs[t] < 0.03:
            continue
        plines = {}
        for f, l in r[t]["plines"]:
            plines.setdefault(f, set()).add(l)
        fi = r.get("fileimport:" + files[i])
        if fi:
            for f, l in fi["lines"]:
                plines.setdefault(f, set()).add(l)
        acc = {}
        for k, u in enumerate(us):
            bit = 1 << k
            for f, l in u:
                key = (f, l)
                acc[key] = acc.get(key, 0) | bit
        masks = []
        mid = {}
        ulines = {}
        for (f, l), m in acc.items():
            j = mid.get(m)
            if j is None:
                j = mid[m] = len(masks)
                masks.append(m)
            ulines.setdefault(f, {})[l] = j
        units[i] = (len(us), {f: frozenset(v) for f, v in plines.items()}, ulines, masks)
    blob = zlib.compress(pickle.dumps(units, protocol=5), 9)
    open(sys.argv[4], "wb").write(blob)
    print("units:", len(units), "tests,", len(blob), "bytes")

# ---- instruction-level coverage (which tests execute which bytecode offsets)
if len(sys.argv) > 5:
    from array import array

    codes = None
    for f in sorted(glob.glob(rawdir + "/s*.pickle")):
        part = pickle.load(open(f, "rb"))
        if codes is None:
            codes = part["__codes__"]
        elif codes != part["__codes__"]:
            raise SystemExit("shards disagree on code numbering")
    acc = {}  # (code number, offset // 2) -> bitset
    for i, t in enumerate(tests):
        bit = 1 << i
        for n, raw in (r[t].get("instr") or {}).items():
            a = array("H")
            a.frombytes(raw)
            for off in a:
                key = (n, off)
                acc[key] = acc.get(key, 0) | bit
    sets = [0]
    sid = {0: 0}
    per_code = {}
    for (n, off), b in acc.items():
        j = sid.get(b)
        if j is None:
            j = sid[b] = len(sets)
            sets.append(b)
        per_code.setdefault(n, []).append((off, j))
    table = {}
    for n, items in per_code.items():
        items.sort()
        table[tuple(codes[n])] = (array("H", [o for o, _ in items]).tobytes(), array("I", [j for _, j in items]).tobytes())
    blob = zlib.compress(pickle.dumps({"codes": table, "sets": sets, "known": [tuple(c) for c in codes]}, protocol=5), 9)
    open(sys.argv[5], "wb").write(blob)
    print("instr:", len(table), "code objects,", len(acc), "offsets,", len(sets), "sets,", len(blob), "bytes")
