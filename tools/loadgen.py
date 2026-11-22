"""Which sqlglot modules does each test cause to be imported?

sqlglot loads its dialects lazily, so a module that stops importing only
breaks the tests that ask for it. Recorded on the pinned commit: every test
runs in a fresh fork of a process that has imported nothing but `sqlglot`
itself, and reports the sqlglot modules that got loaded by importing its test
file and running it.

usage: loadgen.py <repo> <tests.json> <out.json> <shard> <nshards>
"""
import importlib
import json
import os
import sys
import unittest

repo, tests_json, out, shard, nshards = sys.argv[1:6]
shard, nshards = int(shard), int(nshards)
repo = os.path.realpath(repo)
os.environ["SKIP_INTEGRATION"] = "0"
sys.path.insert(0, repo)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(repo)
import fakepool

fakepool.install()
import duckdb  # noqa: F401  (heavy imports once, before forking)
import pandas  # noqa: F401

import sqlglot  # noqa: F401

base = {n for n in sys.modules if n == "sqlglot" or n.startswith("sqlglot.")}
tests = json.load(open(tests_json))["tests"]
files = []
for t in tests:
    if t["file"] not in files:
        files.append(t["file"])
files = [f for i, f in enumerate(files) if i % nshards == shard]
result = {"__base__": sorted(base)}
for t in tests:
    if t["file"] not in files:
        continue
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            devnull = os.open(os.devnull, os.O_RDWR)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            f, cls, meth = t["id"].split("::")
            mod = importlib.import_module(f[:-3].replace("/", "."))
            case = getattr(mod, cls)(meth)
            res = unittest.TestResult()
            unittest.TestSuite([case]).run(res)
            loaded = sorted(n for n in sys.modules if (n == "sqlglot" or n.startswith("sqlglot.")) and n not in base)
            with os.fdopen(w, "w") as fh:
                json.dump({"loaded": loaded, "ok": res.wasSuccessful()}, fh)
        finally:
            os._exit(0)
    os.close(w)
    with os.fdopen(r) as fh:
        data = fh.read()
    os.waitpid(pid, 0)
    try:
        d = json.loads(data)
        result[t["id"]] = d["loaded"]
        if not d["ok"]:
            print("FAILED", t["id"], file=sys.stderr)
    except Exception as e:
        print("NO RESULT", t["id"], repr(e), file=sys.stderr)
json.dump(result, open(out, "w"))
