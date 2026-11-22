"""Build raw per-test maps from the pinned sqlglot checkout.

For every test in tests.json records, on the unmodified pinned commit:
  - which source lines of sqlglot/ and tests/ it executes
  - which sqlglot classes it instantiates
  - which repo files it opens
  - which sqlglot modules it asks the import system for
Also records the same for "import sqlglot" and for importing each test file.

usage: mapgen.py <repo> <tests.json> <out.pickle> <shard> <nshards>
"""
import builtins
import enum
import importlib
import io
import json
import os
import pickle
import pkgutil
import sys
import time
import unittest

repo, tests_json, out, shard, nshards = sys.argv[1:6]
shard, nshards = int(shard), int(nshards)
repo = os.path.realpath(repo)
os.environ["SKIP_INTEGRATION"] = "0"
sys.path.insert(0, repo)
os.chdir(repo)
PREFIX = repo + os.sep

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fakepool

cur_units = []
cur_plines = set()  # lines the test process ran outside any unit's window
cur_extra = {}  # unit index -> lines the test process ran while handling that unit
cur_unit = [None]


def _flush_window(next_unit):
    """Lines since the last boundary belong to the unit being handled, if any."""
    got = set(cur_lines)
    cur_lines.clear()
    if cur_unit[0] is None:
        cur_plines.update(got)
    else:
        cur_extra.setdefault(cur_unit[0], set()).update(got)
    cur_unit[0] = next_unit
    mon.restart_events()


import concurrent.futures as _cf


class UnitFuture(_cf.Future):
    """Asking for a unit's result opens that unit's window in the test process."""

    unit_index = None

    def result(self, timeout=None):
        if self.unit_index is not None:
            _flush_window(self.unit_index)
        return super().result(timeout)


class WindowResult(unittest.TestResult):
    """The end of a subtest closes the window of the unit it was checking."""

    def addSubTest(self, test, subtest, err):
        _flush_window(None)
        super().addSubTest(test, subtest, err)


class TracingForkPool(fakepool.SyncPool):
    """Runs every submitted unit in a forked child so each unit is traced cold."""

    def submit(self, fn, /, *args, **kwargs):
        f = UnitFuture()
        f.unit_index = len(cur_units)
        if self._broken is not None:
            f.set_exception(RuntimeError("initializer failed"))
            return f
        # the real pool pickles the call in the parent and the result in the worker;
        # the parent's share of that work belongs to this unit
        _flush_window(f.unit_index)
        try:
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
        except Exception as e:
            _flush_window(None)
            cur_units.append([])
            f.set_exception(e)
            return f
        _flush_window(None)
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            try:
                snapshot()
                try:
                    val = fn(*args, **kwargs)
                    res = (True, pickle.dumps(val))
                except Exception as e:
                    res = (False, e)
                snap = snapshot()
                try:
                    blob = pickle.dumps((res, snap))
                except Exception as e:
                    blob = pickle.dumps(((False, RuntimeError(repr(res[1]))), snap))
                with os.fdopen(w, "wb") as fh:
                    fh.write(blob)
            finally:
                os._exit(0)
        os.close(w)
        with os.fdopen(r, "rb") as fh:
            blob = fh.read()
        os.waitpid(pid, 0)
        (ok, val), snap = pickle.loads(blob)
        unit_instr.update(snap.pop("instr", ()))
        cur_classes.update(snap["classes"])
        cur_opens.update(snap["opens"])
        cur_imports.update(snap["imports"])
        cur_units.append(snap["lines"])
        if ok:
            # unpickling the result is this unit's work too, done in the test process
            _flush_window(f.unit_index)
            try:
                val = pickle.loads(val)
            finally:
                _flush_window(None)
            f.set_result(val)
        else:
            f.set_exception(val)
        return f


fakepool.SyncPool = TracingForkPool
fakepool.install()

cur_lines = set()
cur_classes = set()
cur_opens = set()
cur_imports = set()

mon = sys.monitoring
TOOL = mon.COVERAGE_ID
mon.use_tool_id(TOOL, "mapgen")
DISABLE = mon.DISABLE
_fname_cache = {}


def on_line(code, line):
    fn = code.co_filename
    rel = _fname_cache.get(fn)
    if rel is None:
        rp = os.path.realpath(fn)
        rel = rp[len(PREFIX):] if rp.startswith(PREFIX) else ""
        if not (rel.startswith("sqlglot/") or rel.startswith("tests/")):
            rel = ""
        _fname_cache[fn] = rel
    if rel:
        cur_lines.add((rel, line))
    return DISABLE


mon.register_callback(TOOL, mon.events.LINE, on_line)
mon.set_events(TOOL, mon.events.LINE)

# Instruction-level coverage of sqlglot's own functions: which bytecode offsets
# of which code object a test executes. Finer than lines: on
# `if a and b.X:` it tells the tests that reach `b.X` from those that stop at `a`.
cur_instr = set()  # (code number, offset)
unit_instr = set()
code_number = {}  # id(code) -> number
code_keys = []  # number -> (file, qualname, first line)


def on_instruction(code, offset):
    n = code_number.get(id(code))
    if n is not None:
        cur_instr.add((n, offset))
    return DISABLE


mon.register_callback(TOOL, mon.events.INSTRUCTION, on_instruction)


def enable_instruction_tracing():
    """Numbers every function code object of sqlglot and turns its events on."""
    import gc
    from types import CodeType, FunctionType

    found = {}

    def add(co):
        rp = os.path.realpath(co.co_filename)
        if not rp.startswith(PREFIX + "sqlglot" + os.sep):
            return
        key = (rp[len(PREFIX):], co.co_qualname, co.co_firstlineno)
        if key in found:
            if found[key] is not co:
                found[key] = None  # two different code objects, same key: leave it to line coverage
            return
        found[key] = co
        for c in co.co_consts:
            if isinstance(c, CodeType):
                add(c)

    for o in gc.get_objects():
        if type(o) is FunctionType:
            add(o.__code__)
    keep = []
    for key in sorted(found):
        co = found[key]
        if co is None:
            continue
        code_number[id(co)] = len(code_keys)
        code_keys.append(key)
        keep.append(co)
        mon.set_local_events(TOOL, co, mon.events.INSTRUCTION)
    return keep


def pack_instr(pairs):
    """{code number: bytes of sorted uint16 (offset // 2)}"""
    from array import array

    d = {}
    for n, off in pairs:
        d.setdefault(n, []).append(off // 2)
    return {n: array("H", sorted(v)).tobytes() for n, v in d.items()}

_orig_open = builtins.open


def _note_path(p):
    try:
        if isinstance(p, int):
            return
        rp = os.path.realpath(os.fspath(p))
        if rp.startswith(PREFIX):
            cur_opens.add(rp[len(PREFIX):])
    except Exception:
        pass


def tracking_open(file, *a, **k):
    _note_path(file)
    return _orig_open(file, *a, **k)


builtins.open = tracking_open
io.open = tracking_open

_orig_import = builtins.__import__
_orig_import_module = importlib.import_module


def tracking_import(name, globals=None, locals=None, fromlist=(), level=0):
    if level == 0:
        if name.startswith("sqlglot") or name.startswith("tests"):
            cur_imports.add(name)
            if fromlist:
                for f in fromlist:
                    cur_imports.add(name + "." + f)
    elif globals is not None:
        pkg = globals.get("__package__") or ""
        if pkg.startswith("sqlglot") or pkg.startswith("tests"):
            parts = pkg.split(".")
            base = ".".join(parts[: len(parts) - level + 1])
            full = base + ("." + name if name else "")
            cur_imports.add(full)
            for f in fromlist or ():
                cur_imports.add(full + "." + f)
    return _orig_import(name, globals, locals, fromlist, level)


def tracking_import_module(name, package=None):
    if name.startswith("sqlglot") or name.startswith("tests"):
        cur_imports.add(name)
    return _orig_import_module(name, package)


builtins.__import__ = tracking_import
importlib.import_module = tracking_import_module


def snapshot():
    r = {
        "lines": sorted(cur_lines),
        "classes": sorted(cur_classes),
        "opens": sorted(cur_opens),
        "imports": sorted(cur_imports),
        "instr": set(cur_instr),
    }
    cur_instr.clear()
    cur_lines.clear()
    cur_classes.clear()
    cur_opens.clear()
    cur_imports.clear()
    mon.restart_events()
    return r


result = {}

# ---- phase 1: plain "import sqlglot"
import sqlglot  # noqa

result["import:sqlglot"] = snapshot()

# ---- phase 2: every sqlglot module (lazy dialects included)
allmods = []
for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if m.name.endswith("__main__"):
        continue
    try:
        importlib.import_module(m.name)
        allmods.append(m.name)
    except Exception as e:  # pragma: no cover
        print("cannot import", m.name, e, file=sys.stderr)
result["import:all"] = snapshot()
_keep_alive = enable_instruction_tracing()
result["__codes__"] = list(code_keys)
for _ctx in ("import:sqlglot", "import:all"):
    result[_ctx].pop("instr", None)


# ---- class instantiation tracking
def _rec(tp):
    for k in tp.__mro__:
        mod = k.__module__
        if mod.startswith("sqlglot"):
            cur_classes.add(mod + ":" + k.__qualname__)


def _wrap_class(K):
    if issubclass(K, enum.Enum):
        return
    own = K.__dict__.get("__init__")
    if own is not None:
        orig = own

        def __init__(self, *a, **k):
            _rec(type(self))
            return orig(self, *a, **k)

    else:
        # only wrap roots: no sqlglot ancestor
        if any(b.__module__.startswith("sqlglot") for b in K.__mro__[1:]):
            return
        orig = K.__init__
        if orig is object.__init__:

            def __init__(self, *a, **k):
                _rec(type(self))

        else:

            def __init__(self, *a, **k):
                _rec(type(self))
                return orig(self, *a, **k)

    try:
        K.__init__ = __init__
    except Exception as e:
        print("cannot wrap", K, e, file=sys.stderr)


seen = set()
for name in list(sys.modules):
    if not name.startswith("sqlglot"):
        continue
    mod = sys.modules[name]
    stack = [v for v in vars(mod).values() if isinstance(v, type)]
    while stack:
        K = stack.pop()
        if K in seen or not getattr(K, "__module__", "").startswith("sqlglot"):
            continue
        seen.add(K)
        stack.extend(v for v in vars(K).values() if isinstance(v, type))
for K in sorted(seen, key=lambda k: len(k.__mro__)):
    _wrap_class(K)
snapshot()

# ---- phase 3: tests
tests = json.load(open(tests_json))["tests"]
files = []
for t in tests:
    if t["file"] not in files:
        files.append(t["file"])
files = [f for i, f in enumerate(files) if i % nshards == shard]

for f in files:
    modname = f[:-3].replace("/", ".")
    try:
        mod = importlib.import_module(modname)
    except Exception as e:
        print("IMPORT FAIL", f, repr(e), file=sys.stderr)
        continue
    result["fileimport:" + f] = snapshot()
    result["fileimport:" + f].pop("instr", None)
    for t in tests:
        if t["file"] != f:
            continue
        _, cls, meth = t["id"].split("::")
        tmp = out + ".%d.tmp" % os.getpid()
        pid = os.fork()
        if pid == 0:
            try:
                case = getattr(mod, cls)(meth)
                res = WindowResult()
                snapshot()
                del cur_units[:]
                cur_plines.clear()
                cur_extra.clear()
                cur_unit[0] = None
                unit_instr.clear()
                t0 = time.process_time()
                unittest.TestSuite([case]).run(res)
                dt = time.process_time() - t0
                _flush_window(None)
                s = snapshot()
                s["secs"] = dt
                units = []
                allu = set(cur_plines)
                for k, u in enumerate(cur_units):
                    lines = set(u) | cur_extra.get(k, set())
                    units.append(sorted(lines))
                    allu.update(lines)
                s["units"] = units
                s["instr"] = pack_instr(s["instr"] | unit_instr)
                s["plines"] = sorted(cur_plines)
                s["lines"] = sorted(allu)
                s["ok"] = res.wasSuccessful() and not res.skipped
                if not res.wasSuccessful():
                    print("FAILED", t["id"], (res.failures + res.errors)[0][1][-600:], file=sys.stderr)
                if res.skipped:
                    print("SKIPPED", t["id"], res.skipped, file=sys.stderr)
                with _orig_open(tmp, "wb") as fh:
                    pickle.dump(s, fh)
            finally:
                sys.stderr.flush()
                os._exit(0)
        os.waitpid(pid, 0)
        try:
            with _orig_open(tmp, "rb") as fh:
                result[t["id"]] = pickle.load(fh)
            os.unlink(tmp)
        except Exception as e:
            print("NO RESULT", t["id"], repr(e), file=sys.stderr)
    print("done", f, file=sys.stderr)

with _orig_open(out, "wb") as fh:
    pickle.dump(result, fh)
