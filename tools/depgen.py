"""Which sqlglot modules does importing each sqlglot module depend on?

Recorded on the pinned commit by watching the imports happen: module B is a
dependency of module A when, while A's body runs, A imports B (at any depth,
lazily or not) or code that lives in B is executed.

usage: depgen.py <repo> <out.json>
"""
import builtins
import importlib
import json
import os
import pkgutil
import sys

repo, out = sys.argv[1:3]
repo = os.path.realpath(repo)
sys.path.insert(0, repo)
os.chdir(repo)
PRE = os.path.join(repo, "sqlglot") + os.sep

deps = {}
stack = []
_modname = {}


def modname(fn):
    m = _modname.get(fn)
    if m is None:
        rp = os.path.realpath(fn)
        if rp.startswith(PRE) and rp.endswith(".py"):
            m = "sqlglot." + rp[len(PRE):-3].replace(os.sep, ".")
            if m.endswith(".__init__"):
                m = m[: -len(".__init__")]
        else:
            m = ""
        _modname[fn] = m
    return m


mon = sys.monitoring
TOOL = mon.PROFILER_ID
mon.use_tool_id(TOOL, "depgen")


def on_start(code, offset):
    m = modname(code.co_filename)
    if not m:
        return mon.DISABLE
    if code.co_name == "<module>":
        stack.append(m)
        deps.setdefault(m, set())
        mon.restart_events()
        return None
    if stack and stack[-1] != m:
        deps[stack[-1]].add(m)
    return mon.DISABLE


def on_return(code, offset, retval):
    m = modname(code.co_filename)
    if m and code.co_name == "<module>":
        if stack and stack[-1] == m:
            stack.pop()
        mon.restart_events()
        return None
    return mon.DISABLE


def on_unwind(code, offset, exc):
    m = modname(code.co_filename)
    if m and code.co_name == "<module>" and stack and stack[-1] == m:
        stack.pop()
        mon.restart_events()


mon.register_callback(TOOL, mon.events.PY_START, on_start)
mon.register_callback(TOOL, mon.events.PY_RETURN, on_return)
mon.register_callback(TOOL, mon.events.PY_UNWIND, on_unwind)
mon.set_events(TOOL, mon.events.PY_START | mon.events.PY_RETURN | mon.events.PY_UNWIND)

_orig_import = builtins.__import__
_orig_import_module = importlib.import_module


def note(name):
    if stack and name and (name == "sqlglot" or name.startswith("sqlglot.")):
        while name:
            if name != stack[-1]:
                deps[stack[-1]].add(name)
            name = name.rpartition(".")[0]


def tracking_import(name, globals=None, locals=None, fromlist=(), level=0):
    mod = _orig_import(name, globals, locals, fromlist, level)
    if stack:
        if level and globals is not None:
            pkg = (globals.get("__package__") or "").split(".")
            base = ".".join(pkg[: len(pkg) - level + 1])
            full = base + ("." + name if name else "")
        else:
            full = name
        note(full)
        for f in fromlist or ():
            if (full + "." + f) in sys.modules:
                note(full + "." + f)
    return mod


def tracking_import_module(name, package=None):
    mod = _orig_import_module(name, package)
    note(getattr(mod, "__name__", ""))
    return mod


builtins.__import__ = tracking_import
importlib.import_module = tracking_import_module

import sqlglot  # noqa

names = ["sqlglot"]
for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if m.name.endswith("__main__"):
        continue
    importlib.import_module(m.name)
    names.append(m.name)
mon.set_events(TOOL, 0)

# objects a module holds that were defined elsewhere (belt and braces)
for n in list(deps):
    mod = sys.modules.get(n)
    if mod is None or n == "sqlglot.dialects":
        continue
    for v in list(vars(mod).values()):
        if isinstance(v, type(sys)):
            continue
        name = getattr(v, "__module__", None)
        if isinstance(name, str) and name != n and name in deps:
            deps[n].add(name)
    parent = n.rpartition(".")[0]
    if parent in deps:
        deps[n].add(parent)

json.dump({k: sorted(v) for k, v in sorted(deps.items())}, open(out, "w"))
print(len(deps), "modules", sum(len(v) for v in deps.values()), "edges", file=sys.stderr)
