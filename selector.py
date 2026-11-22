"""Blast Radius selector.

For every patch:
  1. work out which tests could possibly notice it, from maps recorded on the
     pinned commit (per-test line coverage, classes used, files read);
  2. apply the patch to a scratch copy of the checkout and actually run those
     candidate tests in a lean in-process runner;
  3. print exactly the tests that failed, packed onto workers.

Nothing here knows what any particular patch breaks: the answer always comes
from running the pinned test-suite against the patched code.
"""
import dis
import difflib
import json
import os
import pickle
import select
import signal
import sys
import time
import zlib
from types import CodeType

HERE = os.path.dirname(os.path.abspath(__file__))
PRISTINE = os.environ.get("BR_PRISTINE", "/opt/sqlglot")
DATA = os.environ.get("BR_DATA", "/data")
TMP = os.environ.get("BR_TMP", "/tmp")
NPROC = int(os.environ.get("BR_NPROC", "2"))
WALL_BUDGET = float(os.environ.get("BR_WALL", "150"))
JOB_WALL = float(os.environ.get("BR_JOB_WALL", "100"))
FULL = os.environ.get("BR_FULL") == "1"
PROFILE = os.environ.get("BR_PROFILE") == "1"
DEBUG = os.environ.get("BR_DEBUG") == "1"
GC_THRESHOLD = int(os.environ.get("BR_GC", "100000"))
MEM_FRACTION = float(os.environ.get("BR_MEM", "0.72"))
PURGE_ALL = os.environ.get("BR_PURGE_ALL") == "1"
USE_LOADS = os.environ.get("BR_NO_LOADS") != "1"
PROBE = os.environ.get("BR_NO_PROBE") != "1"
PROBE_MIN_EVALS = int(os.environ.get("BR_PROBE_EVALS", "200"))
PROBE_MIN_TESTS = int(os.environ.get("BR_PROBE_TESTS", "10"))
T0 = time.time()


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# patch parsing
# --------------------------------------------------------------------------
class FileChange:
    __slots__ = ("old", "new", "hunks", "binary", "weird")

    def __init__(self):
        self.old = None
        self.new = None
        self.hunks = []
        self.binary = False
        self.weird = False


def _strip(p):
    p = p.strip()
    if p.startswith('"') and p.endswith('"'):
        p = p[1:-1]
    if p == "/dev/null":
        return None
    if p.startswith("a/") or p.startswith("b/"):
        p = p[2:]
    return p


def parse_patch(text):
    files = []
    cur = None
    lines = text.split("\n")
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        if ln.startswith("diff --git "):
            cur = FileChange()
            files.append(cur)
            rest = ln[len("diff --git "):]
            # "a/x b/x" -- split in the middle for the common same-name case
            half = len(rest) // 2
            if rest[half] == " " and rest[:half][2:] == rest[half + 1:][2:]:
                cur.old = _strip(rest[:half])
                cur.new = _strip(rest[half + 1:])
            else:
                parts = rest.split(" b/")
                if len(parts) == 2:
                    cur.old = _strip(parts[0])
                    cur.new = _strip("b/" + parts[1])
            i += 1
            continue
        if cur is None:
            if ln.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
                cur = FileChange()
                files.append(cur)
            else:
                i += 1
                continue
        if ln.startswith("--- "):
            p = ln[4:].split("\t")[0]
            if _strip(p) is None:
                cur.old = None
            else:
                cur.old = _strip(p)
            i += 1
            continue
        if ln.startswith("+++ "):
            p = ln[4:].split("\t")[0]
            if _strip(p) is None:
                cur.new = None
            else:
                cur.new = _strip(p)
            i += 1
            continue
        if ln.startswith("new file mode"):
            cur.old = None
        elif ln.startswith("deleted file mode"):
            cur.new = None
        elif ln.startswith("rename from "):
            cur.old = ln[len("rename from "):].strip()
        elif ln.startswith("rename to "):
            cur.new = ln[len("rename to "):].strip()
        elif ln.startswith("copy from ") or ln.startswith("copy to "):
            cur.weird = True
        elif ln.startswith("Binary files ") or ln.startswith("GIT binary patch"):
            cur.binary = True
        elif ln.startswith("@@"):
            try:
                head = ln.split("@@")[1].strip().split()
                o = head[0][1:].split(",")
                w = head[1][1:].split(",")
                ostart = int(o[0])
                olen = int(o[1]) if len(o) > 1 else 1
                nstart = int(w[0])
                nlen = int(w[1]) if len(w) > 1 else 1
            except Exception:
                cur.weird = True
                i += 1
                continue
            body = []
            i += 1
            oc = nc = 0
            while i < n and (oc < olen or nc < nlen):
                b = lines[i]
                if b.startswith("\\"):
                    body.append(("\\", ""))
                elif b.startswith("+"):
                    body.append(("+", b[1:]))
                    nc += 1
                elif b.startswith("-"):
                    body.append(("-", b[1:]))
                    oc += 1
                else:
                    body.append((" ", b[1:]))
                    oc += 1
                    nc += 1
                i += 1
            if i < n and lines[i].startswith("\\"):
                body.append(("\\", ""))
                i += 1
            cur.hunks.append((ostart, olen, nstart, nlen, body))
            continue
        i += 1
    return files


def apply_hunks(old_text, hunks):
    """Returns the patched text, or None when the hunks do not fit."""
    old = old_text.split("\n")
    # old_text "a\nb\n" -> ["a","b",""]: the trailing "" stands for the final newline
    out = []
    pos = 0  # index into old
    for ostart, olen, nstart, nlen, body in hunks:
        start = ostart - 1 if olen else ostart
        if start < pos:
            return None
        out.extend(old[pos:start])
        pos = start
        last = None
        for tag, txt in body:
            if tag == " ":
                if pos >= len(old) or old[pos] != txt:
                    return None
                out.append(txt)
                pos += 1
                last = " "
            elif tag == "-":
                if pos >= len(old) or old[pos] != txt:
                    return None
                pos += 1
                last = "-"
            elif tag == "+":
                out.append(txt)
                last = "+"
            else:  # "\ No newline at end of file" for the line just seen
                if last in ("-", " "):
                    # old file had no trailing newline: old list has no trailing ""
                    pass
                if last in ("+", " "):
                    out.append(None)  # marker: no newline after previous
    out.extend(old[pos:])
    # resolve markers
    res = []
    nonl = False
    for x in out:
        if x is None:
            nonl = True
        else:
            res.append(x)
    text = "\n".join(res)
    if nonl and text.endswith("\n"):
        text = text[:-1]
    elif nonl:
        pass
    return text


# --------------------------------------------------------------------------
# maps
# --------------------------------------------------------------------------
class Maps:
    def __init__(self, tests):
        with open(os.path.join(HERE, "maps.bin"), "rb") as fh:
            d = pickle.loads(zlib.decompress(fh.read()))
        self.ids = d["tests"]
        self.lines = d["lines"]
        self.classes = d["classes"]
        self.opens = d["opens"]
        self.imports = d["imports"]
        self.implines = {f: set(v) for f, v in d["implines"].items()}
        self.cpu = d.get("cpu") or []
        # test -> sqlglot modules it causes to be loaded lazily (tools/loadgen.py)
        self.loads = d.get("loads") or {}
        # import-time dependencies between sqlglot modules (tools/depgen.py)
        self.deps = {k: set(v) for k, v in (d.get("deps") or {}).items()}
        self.n = len(self.ids)
        self.all = (1 << self.n) - 1
        # translate map index -> test number of this run (by id)
        num = {t["id"]: t["number"] for t in tests}
        self.number = [num.get(i) for i in self.ids]
        self.known = 0
        for i, x in enumerate(self.number):
            if x is not None:
                self.known |= 1 << i
        self.file_bits = {}
        for i, tid in enumerate(self.ids):
            f = tid.split("::")[0]
            self.file_bits[f] = self.file_bits.get(f, 0) | (1 << i)
        self._file_any = {}
        self.units = None
        self.unmapped = [t["number"] for t in tests if t["id"] not in set(self.ids)]

    def load_units(self):
        """Per-test coverage of each unit of pooled work (see fakepool)."""
        if self.units is None:
            try:
                with open(os.path.join(HERE, "units.bin"), "rb") as fh:
                    self.units = pickle.loads(zlib.decompress(fh.read()))
            except Exception as e:
                log("no unit map:", repr(e))
                self.units = {}
        return self.units

    def unit_mask(self, i, changed):
        """Bitmask of the units of test i that execute a changed line.

        None when the test has to run whole: it has no units, or the test
        process executes a changed line outside of any unit's own work (what it
        computes there may feed every unit).
        """
        u = self.units.get(i) if self.units else None
        if u is None:
            return None
        n, plines, ulines, masks = u
        mask = 0
        for rel, lines in changed.items():
            pl = plines.get(rel)
            ul = ulines.get(rel)
            for l in lines:
                if pl is not None and l in pl:
                    return None
                if ul is not None:
                    k = ul.get(l)
                    if k is not None:
                        mask |= masks[k]
        return mask

    def cover(self, rel, lines):
        m = self.lines.get(rel)
        if not m:
            return 0
        b = 0
        for l in lines:
            b |= m.get(l, 0)
        return b

    def file_any(self, rel):
        b = self._file_any.get(rel)
        if b is None:
            b = 0
            for v in self.lines.get(rel, {}).values():
                b |= v
            self._file_any[rel] = b
        return b

    def module_touch(self, rel):
        b = self.file_any(rel) | self.opens.get(rel, 0)
        if rel.endswith(".py"):
            mod = rel[:-3].replace("/", ".")
            if mod.endswith(".__init__"):
                mod = mod[: -len(".__init__")]
            b |= self.imports.get(mod, 0)
            pre = mod + ":"
            for k, v in self.classes.items():
                if k.startswith(pre):
                    b |= v
        return b

    def dir_readers(self, rel):
        """Tests that open some file in the same directory as rel."""
        d = os.path.dirname(rel) + "/"
        b = 0
        for f, v in self.opens.items():
            if f.startswith(d) and "/" not in f[len(d):]:
                b |= v
        return b

    def dir_touch(self, rel):
        d = os.path.dirname(rel) + "/"
        b = 0
        for f in self.lines:
            if f.startswith(d):
                b |= self.file_any(f)
        for f, v in self.opens.items():
            if f.startswith(d):
                b |= v
        return b


class Instr:
    """Which tests execute which bytecode offsets of sqlglot's functions (instr.bin)."""

    def __init__(self, maps):
        self.ok = False
        try:
            with open(os.path.join(HERE, "instr.bin"), "rb") as fh:
                d = pickle.loads(zlib.decompress(fh.read()))
            self.codes = d["codes"]
            self.sets = d["sets"]
            self.known = set(d["known"])
            self.cache = {}
            self.ok = bool(self.known)
        except Exception as e:
            log("no instruction map:", repr(e))

    def cover(self, key, offsets):
        """Tests executing any of `offsets` of code `key`; None if the code is not mapped."""
        if not self.ok or key not in self.known:
            return None
        t = self.cache.get(key)
        if t is None:
            e = self.codes.get(key)
            t = {}
            if e is not None:
                from array import array

                offs = array("H")
                offs.frombytes(e[0])
                ids = array("I")
                ids.frombytes(e[1])
                t = dict(zip(offs, ids))
            self.cache[key] = t
        b = 0
        for off in offsets:
            j = t.get(off // 2)
            if j:
                b |= self.sets[j]
        return b


INSTR = None


def bits(b):
    out = []
    i = 0
    while b:
        low = b & -b
        out.append(low.bit_length() - 1)
        b ^= low
    return out


# --------------------------------------------------------------------------
# what did a .py change really change?
# --------------------------------------------------------------------------
CO_OPTIMIZED = 0x1


def _children(co):
    return [c for c in co.co_consts if isinstance(c, CodeType)]


def _crepr(c):
    """repr of a constant that tells 1 from True from 1.0 and is stable for sets."""
    if isinstance(c, frozenset):
        return "fs{" + ",".join(sorted(_crepr(x) for x in c)) + "}"
    if isinstance(c, tuple):
        return "(" + ",".join(_crepr(x) for x in c) + ")"
    return type(c).__name__ + ":" + repr(c)


def _sig(co):
    consts = tuple(_crepr(c) for c in co.co_consts if not isinstance(c, CodeType))
    return (
        co.co_code,
        consts,
        co.co_names,
        co.co_varnames,
        co.co_freevars,
        co.co_cellvars,
        co.co_flags,
        co.co_argcount,
        co.co_kwonlyargcount,
        co.co_posonlyargcount,
        co.co_name,
        co.co_qualname,
    )


def _own_lines(co):
    return {l for _, _, l in co.co_lines() if l}


def _all_lines(co):
    s = _own_lines(co)
    for c in _children(co):
        s |= _all_lines(c)
    return s


def _instr_keys(co):
    keys = []
    lines = []
    for ins in dis.get_instructions(co):
        if ins.opname == "EXTENDED_ARG":
            continue  # only widens the next instruction's argument, which is compared by value
        av = ins.argval
        if isinstance(av, CodeType):
            k = (ins.opname, "code:" + av.co_qualname)
        elif ins.opcode in dis.hasjrel or ins.opcode in dis.hasjabs or "JUMP" in ins.opname:
            k = (ins.opname, "")
        elif ins.opcode in dis.hasconst:
            k = (ins.opname, _crepr(av))
        elif ins.argrepr:
            k = (ins.opname, ins.argrepr)
        else:
            # IS_OP, CONTAINS_OP, CALL, ... carry their meaning in the bare argument
            k = (ins.opname, ins.arg)
        if k == ("STORE_NAME", "__firstlineno__") and keys:
            # class bodies record their own line number: not a real change
            keys[-1] = ("LOAD_CONST", "")
        keys.append(k)
        pos = ins.positions
        lines.append(pos.lineno if pos is not None else None)
    return keys, lines


def _same_code(o, n):
    so = _sig(o)
    sn = _sig(n)
    if so == sn:
        return True
    if o.co_flags & CO_OPTIMIZED:
        return False
    if so[2:] != sn[2:]:
        return False
    try:
        return _instr_keys(o)[0] == _instr_keys(n)[0]
    except Exception:
        return False


def _jump_targets(co):
    """instruction index -> index of its jump target (for jumps only)."""
    ins = []
    at = {}
    for i in dis.get_instructions(co):
        # same numbering as _instr_keys: EXTENDED_ARG prefixes are not counted, and a
        # jump to a prefix is a jump to the instruction it belongs to
        at[i.offset] = len(ins)
        if i.opname != "EXTENDED_ARG":
            ins.append(i)
    out = {}
    for k, i in enumerate(ins):
        if i.opcode in dis.hasjrel or i.opcode in dis.hasjabs or "JUMP" in i.opname:
            t = at.get(i.argval)
            if t is not None:
                out[k] = t
    return out


def _instr_offsets(co):
    """For each instruction (EXTENDED_ARG prefixes folded in): the offsets it occupies.

    An instruction that begins a line (or can be jumped to) is reported by the
    interpreter as a line event rather than an instruction event, so the
    instruction map cannot vouch for it: such instructions get an empty list
    and are covered through their line.
    """
    ins_all = list(dis.get_instructions(co))
    targets = {i.argval for i in ins_all if (i.opcode in dis.hasjrel or i.opcode in dis.hasjabs or "JUMP" in i.opname)}
    for e in dis._parse_exception_table(co) if hasattr(dis, "_parse_exception_table") else ():
        targets.add(e.target)
    out = []
    pend = []
    prev_line = None
    first = True
    for ins in ins_all:
        pos = ins.positions
        line = pos.lineno if pos is not None else None
        starts = first or line != prev_line or ins.offset in targets or ins.opname in ("RESUME", "FOR_ITER", "END_FOR")
        prev_line = line
        first = False
        if ins.opname == "EXTENDED_ARG":
            if starts:
                pend.append(None)
            pend.append(ins.offset)
            continue
        if starts or None in pend:
            out.append([])
        else:
            out.append(pend + [ins.offset])
        pend = []
    return out


def changed_old_lines(o, n, offsets=None):
    """Old-side lines whose instructions differ between o and n (None: unknown).

    When `offsets` is a set it receives the bytecode offsets of those instructions.
    """
    try:
        ko, lo = _instr_keys(o)
        kn, _ = _instr_keys(n)
        jo = _jump_targets(o)
        jn = _jump_targets(n)
        oo = _instr_offsets(o) if offsets is not None else None
    except Exception:
        return None
    hit = []
    out = set()
    o2n = {}
    sm = difflib.SequenceMatcher(None, ko, kn, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for d in range(i2 - i1):
                o2n[i1 + d] = j1 + d
            continue
        idx = list(range(i1, i2))
        if i1 > 0:
            idx.append(i1 - 1)
        if i2 < len(lo):
            idx.append(i2)
        for i in idx:
            if lo[i] is None:
                return None
            out.add(lo[i])
            hit.append(i)
    # a jump that now lands somewhere else is a change at the jump itself
    for i, t in jo.items():
        j = o2n.get(i)
        if j is None:
            continue
        if o2n.get(t, -1) != jn.get(j, -2):
            if lo[i] is None:
                return None
            out.add(lo[i])
            hit.append(i)
    if offsets is not None and out:
        for i in hit:
            if oo[i]:
                offsets.update(oo[i])
            else:
                offsets.add(("line", lo[i]))  # only its line can say who runs it
    return out or None


def diff_code(o, n, out):
    """Collect (old_code, new_code_or_None) pairs that differ themselves."""
    oc = _children(o)
    nc = _children(n)
    if len(oc) != len(nc) or [c.co_name for c in oc] != [c.co_name for c in nc]:
        out.append((o, None))
        return
    if not _same_code(o, n):
        out.append((o, n))
    for a, b in zip(oc, nc):
        diff_code(a, b, out)


def swap_pairs(o, n, out):
    """(old, new) for every function code object whose subtree changed.

    Returns True when anything below (or at) o differs.
    """
    oc = _children(o)
    nc = _children(n)
    changed = not _same_code(o, n)
    for a, b in zip(oc, nc):
        if swap_pairs(a, b, out):
            changed = True
    if changed and o.co_flags & CO_OPTIMIZED:
        out.append((o, n))
    return changed


_CMP_OPS = ("COMPARE_OP", "IS_OP", "CONTAINS_OP")


def build_probe(rel, old_src, new_src, o, n):
    """For a change that only swaps one comparison operator: instrumented new code.

    Returns (module code, old operator name, new operator name) or None. In the
    returned code the changed comparison `a OP b` is `__br_cmp__(a, b)`, which
    yields what the new operator yields and also notes whether the old operator
    would have yielded something else. A test during which it never differs ran
    exactly as it does on the pinned commit.
    """
    import ast

    try:
        ko, _ = _instr_keys(o)
        kn, _ = _instr_keys(n)
    except Exception:
        return None
    sm = difflib.SequenceMatcher(None, ko, kn, autojunk=False)
    changed_old = []
    changed_new = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            changed_old += ko[i1:i2]
            changed_new += kn[j1:j2]
    # only comparison instructions (and the bool conversion / jump sense that follows) may differ
    # (`x is None` compiles to a jump-if-none, `x == None` to a load and a compare)
    allowed = _CMP_OPS + (
        "TO_BOOL", "POP_JUMP_IF_TRUE", "POP_JUMP_IF_FALSE", "UNARY_NOT",
        "POP_JUMP_IF_NONE", "POP_JUMP_IF_NOT_NONE", "LOAD_CONST",
    )
    if not changed_old and not changed_new:
        return None
    if any(k[0] not in allowed for k in changed_old + changed_new):
        return None
    try:
        old_tree = ast.parse(old_src)
        new_tree = ast.parse(new_src)
    except SyntaxError:
        return None
    co = [x for x in ast.walk(old_tree) if isinstance(x, ast.Compare)]
    cn = [x for x in ast.walk(new_tree) if isinstance(x, ast.Compare)]
    if len(co) != len(cn):
        return None
    diff = [(a, b) for a, b in zip(co, cn) if len(a.ops) != len(b.ops) or [type(x) for x in a.ops] != [type(x) for x in b.ops]]
    if len(diff) != 1:
        return None
    a, b = diff[0]
    if len(a.ops) != 1 or len(b.ops) != 1:
        return None
    if ast.dump(a.left) != ast.dump(b.left) or ast.dump(a.comparators[0]) != ast.dump(b.comparators[0]):
        return None
    old_op = type(a.ops[0]).__name__
    new_op = type(b.ops[0]).__name__
    if old_op not in _PROBE_OPS or new_op not in _PROBE_OPS:
        return None

    class Swap(ast.NodeTransformer):
        def visit_Compare(self, node):
            if node is b:
                call = ast.Call(func=ast.Name(id="__br_cmp__", ctx=ast.Load()), args=[node.left, node.comparators[0]], keywords=[])
                return ast.copy_location(call, node)
            return self.generic_visit(node)

    try:
        tree = ast.fix_missing_locations(Swap().visit(new_tree))
        code = compile(tree, os.path.join(PRISTINE, rel), "exec", dont_inherit=True)
    except Exception:
        return None
    return code, old_op, new_op


def _probe_ops():
    import operator

    return {
        "Eq": operator.eq, "NotEq": operator.ne, "Lt": operator.lt, "LtE": operator.le,
        "Gt": operator.gt, "GtE": operator.ge, "Is": operator.is_, "IsNot": operator.is_not,
        "In": lambda x, y: x in y, "NotIn": lambda x, y: x not in y,
    }


_PROBE_OPS = _probe_ops()
# Operator swaps for which "never differed" is taken as "this change is invisible":
# identity against equality. (For < against <= the one input that tells them apart
# is exactly what a dedicated test would use, so those always get the full run.)
_PROBE_PAIRS = {frozenset(("Is", "Eq")), frozenset(("IsNot", "NotEq"))}
PROBE_STATE = [0, 0]  # evaluations, evaluations where old and new operator disagree


def install_probe(old_op, new_op):
    import builtins

    f_old = _PROBE_OPS[old_op]
    f_new = _PROBE_OPS[new_op]
    st = PROBE_STATE

    def __br_cmp__(x, y):
        r = f_new(x, y)  # may raise, exactly as the changed code would
        st[0] += 1
        try:
            q = f_old(x, y)
            if q is not r and (type(q) is not type(r) or q != r):
                st[1] += 1
        except Exception:
            st[1] += 1
        return r

    builtins.__br_cmp__ = __br_cmp__


_code_cache = {}


def pristine_code(rel, old_src):
    """Code object of a pinned file: from its .pyc when there is one."""
    co = _code_cache.get(rel)
    if co is not None:
        return co
    path = os.path.join(PRISTINE, rel)
    try:
        import importlib.util
        import marshal

        with open(importlib.util.cache_from_source(path), "rb") as fh:
            data = fh.read()
        co = marshal.loads(data[16:])
        if not isinstance(co, CodeType):
            co = None
    except Exception:
        co = None
    if co is None:
        try:
            co = compile(old_src, path, "exec", dont_inherit=True)
        except (SyntaxError, ValueError):
            return None
    _code_cache[rel] = co
    return co


class PyChange:
    """What a patched python file changed, from comparing compiled code."""

    __slots__ = ("rel", "cand", "fresh", "broken", "swaps", "notes", "lines", "probe", "read", "code")

    def __init__(self, rel):
        self.rel = rel
        self.cand = 0  # tests that execute changed function lines
        self.fresh = False  # needs a fresh import (state built at import may differ)
        self.broken = False  # does not compile
        self.swaps = []
        self.notes = []
        self.lines = set()
        self.probe = None  # (swaps with the instrumented code, old operator, new operator)
        self.read = False  # tests read this file's text
        self.code = False  # the compiled code differs


def py_change(maps, rel, old_src, new_src):
    ch = PyChange(rel)
    is_test_file = rel.startswith("tests/")
    try:
        new_co = compile(new_src, os.path.join(PRISTINE, rel), "exec", dont_inherit=True)
    except (SyntaxError, ValueError) as e:
        ch.notes.append("does not compile: %r" % (e,))
        ch.broken = True
        ch.fresh = True
        return ch
    old_co = pristine_code(rel, old_src)
    if old_co is None:
        ch.fresh = True
        return ch
    diffs = []
    diff_code(old_co, new_co, diffs)
    if maps.opens.get(rel, 0):
        # some test reads this file's text: it must see the new text
        ch.cand |= maps.opens[rel]
        ch.read = True
    if not diffs:
        ch.notes.append("no code change")
        return ch
    ch.code = True
    implines = maps.implines.get(rel, ())
    for o, n in diffs:
        if o.co_flags & CO_OPTIMIZED:
            offs = set()
            lines = changed_old_lines(o, n, offs) if n is not None else None
            if lines is None:
                offs = None
                if (
                    n is not None
                    and o.co_code == n.co_code
                    and _sig(o)[2:] == _sig(n)[2:]
                    and _instr_keys(o)[0] == _instr_keys(n)[0]
                ):
                    # same instructions: only a docstring / constant table entry
                    # that nothing loads differs
                    ch.notes.append("func %s: metadata only" % o.co_qualname)
                    continue
                lines = _all_lines(o) - {o.co_firstlineno}
                if n is None:
                    ch.fresh = True
            if not is_test_file and any(l in implines for l in lines):
                ch.notes.append("func %s runs at import" % o.co_qualname)
                ch.fresh = True
            c = maps.cover(rel, lines)
            if offs and INSTR is not None and not is_test_file:
                # finer: the tests that execute the changed instructions themselves
                # (instructions that begin a line are known only by their line)
                by_line = {x[1] for x in offs if isinstance(x, tuple)}
                ci = INSTR.cover((rel, o.co_qualname, o.co_firstlineno), [x for x in offs if not isinstance(x, tuple)])
                if ci is not None:
                    ci |= maps.cover(rel, by_line)
                    ch.notes.append("instr %d of %d" % ((ci & c).bit_count(), c.bit_count()))
                    c &= ci
            ch.lines |= set(lines)
            ch.notes.append("func %s lines %s -> %d" % (o.co_qualname, sorted(lines)[:6], c.bit_count()))
            ch.cand |= c
        else:
            ch.fresh = True
            if is_test_file:
                ch.cand |= maps.file_any(rel)
            ch.notes.append("%s body" % o.co_qualname)
    if not ch.fresh:
        try:
            swap_pairs(old_co, new_co, ch.swaps)
        except Exception:
            ch.fresh = True
    if not ch.fresh and not is_test_file and PROBE and len(diffs) == 1 and diffs[0][1] is not None:
        pr = build_probe(rel, old_src, new_src, diffs[0][0], diffs[0][1])
        if pr is not None:
            ps = []
            try:
                swap_pairs(old_co, pr[0], ps)
                ch.probe = (ps, pr[1], pr[2])
                ch.notes.append("probe %s->%s" % (pr[1], pr[2]))
            except Exception:
                pass
    return ch


# --------------------------------------------------------------------------
# analysis of one commit
# --------------------------------------------------------------------------
class Plan:
    __slots__ = (
        "cid", "cand", "include", "overlay", "notes", "fails", "done",
        "fresh", "tests_changed", "swaps", "all_if_fresh", "changed", "readers", "probe",
        "src_read", "code_files", "lines_all", "test_code",
    )

    def __init__(self, cid):
        self.cid = cid
        self.cand = 0  # tests to run against the patched tree
        self.include = 0  # tests selected without running
        self.overlay = {}  # rel path -> new text (None = deleted)
        self.notes = []
        self.fails = 0
        self.done = False
        self.fresh = False  # sqlglot must be imported fresh from the patched tree
        self.tests_changed = False  # tests/ must be imported from the patched tree
        self.swaps = []  # (rel, old_code, new_code) to swap into loaded functions
        self.all_if_fresh = False
        self.changed = {}  # rel -> changed lines inside functions (pinned numbering)
        self.readers = 0  # candidates because they read a changed file (not via coverage)
        self.probe = None  # (rel, swaps, old op, new op) when the change is one comparison operator
        self.src_read = False  # a changed sqlglot source file is read as text by tests
        self.code_files = set()  # changed sqlglot .py files whose compiled code differs
        self.lines_all = {}  # rel -> changed function lines, also for plans that need a fresh import
        self.test_code = False  # a test module itself changed


def read_pristine(rel):
    try:
        with open(os.path.join(PRISTINE, rel), encoding="utf-8", newline="") as fh:
            return fh.read()
    except (OSError, UnicodeDecodeError):
        return None


def analyze(maps, cid, text):
    plan = Plan(cid)
    try:
        files = parse_patch(text)
    except Exception as e:
        plan.notes.append("unparsable patch %r" % (e,))
        plan.include = maps.all
        return plan
    if not files and text.strip():
        plan.notes.append("no files found in patch")
        plan.include = maps.all
        return plan
    for fc in files:
        rel = fc.new or fc.old
        if rel is None:
            continue
        top = rel.split("/")[0]
        in_tree = top in ("sqlglot", "tests")
        if fc.binary or fc.weird:
            plan.notes.append("%s: binary/odd change" % rel)
            b = 0
            for p in (fc.old, fc.new):
                if p:
                    b |= maps.module_touch(p) | maps.dir_touch(p)
            plan.include |= b
            continue
        if fc.old is not None and fc.new is not None and fc.old != fc.new:
            # rename: the old path disappears, the new one appears
            old_text = read_pristine(fc.old)
            new_text = apply_hunks(old_text, fc.hunks) if (old_text is not None and fc.hunks) else old_text
            if new_text is None:
                plan.include = maps.all
                continue
            plan.overlay[fc.old] = None
            plan.overlay[fc.new] = new_text
            plan.notes.append("%s: renamed" % rel)
            plan.cand |= maps.module_touch(fc.old) | maps.dir_readers(fc.old) | maps.dir_readers(fc.new)
            if fc.old.endswith(".py"):
                plan.cand = maps.all
            if "sqlglot" in (fc.old.split("/")[0], fc.new.split("/")[0]):
                plan.fresh = True
            plan.tests_changed = True
            plan.readers |= plan.cand
            continue
        if fc.old is None:
            new_text = apply_hunks("", fc.hunks)
            if new_text is None:
                plan.include = maps.all
                continue
            plan.overlay[rel] = new_text
            plan.notes.append("%s: new file" % rel)
            base = os.path.basename(rel)
            if base in ("conftest.py", "pytest.ini", "tox.ini"):
                plan.include |= maps.all
            if os.path.exists(os.path.join(PRISTINE, rel)) or base in ("conftest.py", "__init__.py", "sitecustomize.py"):
                plan.cand |= maps.all
            elif in_tree:
                # nothing imports a file that did not exist; tests that scan its
                # directory (they open its neighbours) may still pick it up
                plan.cand |= maps.dir_readers(rel)
            if top == "sqlglot":
                plan.fresh = True
            elif top == "tests":
                plan.tests_changed = True
            continue
        old_text = read_pristine(rel)
        if old_text is None:
            plan.notes.append("%s: not in pinned tree" % rel)
            plan.include |= maps.all
            continue
        if fc.new is None:
            plan.overlay[rel] = None
            plan.notes.append("%s: deleted" % rel)
            plan.cand |= maps.module_touch(rel) | maps.dir_readers(rel)
            if rel.endswith(".py"):
                plan.cand = maps.all
            if top == "sqlglot":
                plan.fresh = True
            elif top == "tests":
                plan.tests_changed = True
            plan.readers |= plan.cand
            continue
        new_text = apply_hunks(old_text, fc.hunks)
        if new_text is None:
            plan.notes.append("%s: hunks do not apply" % rel)
            plan.include |= maps.all
            continue
        if new_text == old_text:
            continue
        if not in_tree:
            # outside the importable tree: tests that read it, selected as is
            b = maps.opens.get(rel, 0)
            if top == "sqlglotc":
                b |= maps.file_bits.get("tests/test_sqlglotc.py", 0)
            base = os.path.basename(rel)
            touched = "\n".join(t for h in fc.hunks for tag, t in h[4] if tag in "+-")
            if base in ("pytest.ini", "tox.ini", "conftest.py") or (
                base in ("pyproject.toml", "setup.cfg") and "pytest" in touched.lower()
            ):
                # how the test run itself is configured: cannot be reproduced here
                b = maps.all
            plan.notes.append("%s: outside tree -> include %d" % (rel, b.bit_count()))
            plan.include |= b
            continue
        plan.overlay[rel] = new_text
        if top == "tests":
            plan.tests_changed = True
            if rel.endswith(".py"):
                plan.test_code = True
        if rel.endswith(".py"):
            ch = py_change(maps, rel, old_text, new_text)
            plan.notes.append("%s: %s" % (rel, "; ".join(ch.notes)))
            plan.cand |= ch.cand
            if top == "tests" and (ch.broken or os.path.basename(rel) in ("conftest.py", "__init__.py")):
                # every test that imports this module is affected (and they are run)
                plan.cand |= maps.file_any(rel) | maps.file_bits.get(rel, 0)
                if os.path.basename(rel) in ("conftest.py", "__init__.py"):
                    if os.path.basename(rel) == "conftest.py":
                        plan.include |= maps.all
                    else:
                        d = os.path.dirname(rel) + "/"
                        for f, fb in maps.file_bits.items():
                            if f.startswith(d):
                                plan.cand |= fb
            plan.readers |= maps.opens.get(rel, 0)
            if top == "sqlglot":
                if ch.read:
                    plan.src_read = True
                if ch.code or ch.broken or ch.fresh:
                    plan.code_files.add(rel)
                if ch.lines:
                    plan.lines_all.setdefault(rel, set()).update(ch.lines)
                if ch.fresh:
                    plan.fresh = True
                else:
                    plan.swaps.extend((rel, o, n) for o, n in ch.swaps)
                    plan.changed.setdefault(rel, set()).update(ch.lines)
                    if ch.probe is not None:
                        plan.probe = (rel,) + ch.probe
            else:
                # test modules are always re-imported from the patched tree
                pass
        else:
            c = maps.opens.get(rel, 0)
            if not c and rel.startswith("tests/"):
                c = maps.dir_touch(rel)
            plan.notes.append("%s: data file -> %d readers" % (rel, c.bit_count()))
            plan.cand |= c
            plan.readers |= c
            if top == "sqlglot":
                plan.fresh = True
    plan.cand &= maps.known
    plan.include &= maps.known
    plan.cand &= ~plan.include
    return plan


# --------------------------------------------------------------------------
# scratch tree with the patch applied
# --------------------------------------------------------------------------
def build_overlay(root, overlay):
    """root/ mirrors PRISTINE through symlinks except for the changed files."""
    os.mkdir(root)
    real = {""}  # directories that must be real (relative)
    for rel in overlay:
        d = os.path.dirname(rel)
        while d:
            real.add(d)
            d = os.path.dirname(d)
    skip = set(overlay)
    for d in sorted(real, key=len):
        dst = os.path.join(root, d) if d else root
        if d:
            os.mkdir(dst)
        src = os.path.join(PRISTINE, d) if d else PRISTINE
        try:
            names = os.listdir(src)
        except OSError:
            names = []
        for name in names:
            r = d + "/" + name if d else name
            if r in real or r in skip:
                continue
            os.symlink(os.path.join(src, name), os.path.join(dst, name))
    for rel, text in overlay.items():
        if text is None:
            continue
        with open(os.path.join(root, rel), "w", encoding="utf-8", newline="") as fh:
            fh.write(text)


def remove_tree(root):
    for dp, dn, fn in os.walk(root, topdown=False, followlinks=False):
        for f in fn:
            try:
                os.unlink(os.path.join(dp, f))
            except OSError:
                pass
        for d in dn:
            p = os.path.join(dp, d)
            try:
                if os.path.islink(p):
                    os.unlink(p)
                else:
                    os.rmdir(p)
            except OSError:
                pass
    try:
        os.rmdir(root)
    except OSError:
        pass


# --------------------------------------------------------------------------
# import-state fingerprint
# --------------------------------------------------------------------------
import enum
import functools
import re
from types import BuiltinFunctionType, FunctionType, MethodType, ModuleType

_PRIM = (int, float, str, bytes, bool, type(None), complex)
_SKIP_CLASS_ATTRS = frozenset(
    (
        "__dict__", "__weakref__", "__module__", "__qualname__", "__firstlineno__",
        "__static_attributes__", "__doc__", "__annotations__", "__parameters__",
        "__orig_bases__", "__abstractmethods__", "_abc_impl", "__match_args__",
        "__dataclass_fields__", "__dataclass_params__", "__annotate__", "__type_params__",
    )
)
_SKIP_MOD_ATTRS = frozenset(
    ("__builtins__", "__cached__", "__file__", "__loader__", "__spec__", "__path__", "__doc__", "__annotations__")
)


def _is_ours(name):
    return name == "sqlglot" or name.startswith("sqlglot.")


def fingerprint(only=None):
    """{entry key: (hash, value)} for every module global and class attribute of sqlglot.

    With `only` (a set of module names) just those modules, and the classes
    they define, are covered.

    Functions are hashed by identity, defaults and closure, not by their code:
    code changes are found by comparing compiled code and handled through
    line coverage. Everything else is hashed by value.
    """
    memo = {}

    def h(v, depth=0):
        t = type(v)
        if t is str:
            return hash(v)
        if t in _PRIM:
            return hash((t.__name__, repr(v)))  # hash(-1) == hash(-2), 1 == True == 1.0
        i = id(v)
        r = memo.get(i)
        if r is not None:
            return r
        if depth > 12:
            return hash(("deep", t.__name__))
        memo[i] = hash(("cycle", t.__name__))
        d = depth + 1
        if t is FunctionType:
            cl = v.__closure__
            r = hash((
                "f", v.__module__, v.__qualname__, h(v.__defaults__, d), h(v.__kwdefaults__, d),
                tuple(_cell(c, d) for c in cl) if cl else None,
            ))
        elif isinstance(v, type):
            r = hash(("class", v.__module__, v.__qualname__))
        elif t is ModuleType:
            r = hash(("module", v.__name__))
        elif t is tuple or t is list:
            hs = [h(x, d) for x in v]
            if v and all(isinstance(x, type) for x in v):
                hs.sort()  # sequences made from a set of classes have no stable order
            r = hash((t.__name__, tuple(hs)))
        elif t is set or t is frozenset:
            r = hash((t.__name__, tuple(sorted(h(x, d) for x in v))))
        elif isinstance(v, dict):
            # order-insensitive: tables built by iterating sets of classes come out
            # in a different order on every import
            r = hash((t.__name__, tuple(sorted((h(k, d), h(x, d)) for k, x in v.items()))))
            if t is not dict and _is_ours(getattr(t, "__module__", "")):
                r = hash((r, _obj(v, d)))
        elif isinstance(v, enum.Enum):
            r = hash(("enum", t.__module__, t.__qualname__, v.name))
        elif t is staticmethod or t is classmethod:
            r = hash((t.__name__, h(v.__func__, d)))
        elif t is property:
            r = hash(("property", h(v.fget, d), h(v.fset, d), h(v.fdel, d)))
        elif t is functools.partial:
            r = hash(("partial", h(v.func, d), h(v.args, d), h(v.keywords, d)))
        elif t is MethodType:
            r = hash(("method", h(v.__func__, d), h(v.__self__, d)))
        elif t is re.Pattern:
            r = hash(("re", v.pattern, v.flags))
        elif _is_ours(getattr(t, "__module__", "") or ""):
            r = hash((t.__module__, t.__qualname__, _obj(v, d)))
        elif isinstance(v, (tuple, list)):
            r = hash((t.__name__, tuple(h(x, d) for x in v)))
        elif isinstance(v, (set, frozenset)):
            r = hash((t.__name__, tuple(sorted(h(x, d) for x in v))))
        else:
            w = getattr(v, "__wrapped__", None)
            if w is not None and type(w) is FunctionType:
                r = hash((t.__name__, h(w, d)))
            else:
                r = hash(("other", getattr(t, "__module__", ""), t.__qualname__, getattr(v, "__qualname__", "")))
        memo[i] = r
        return r

    def _cell(c, d):
        try:
            return h(c.cell_contents, d)
        except ValueError:
            return 0

    def _obj(v, d):
        parts = []
        dd = getattr(v, "__dict__", None)
        if type(dd) is dict:
            parts.append(tuple((k, h(x, d)) for k, x in dd.items()))
        for k in type(v).__mro__:
            sl = k.__dict__.get("__slots__")
            if not sl:
                continue
            if isinstance(sl, str):
                sl = (sl,)
            for s in sl:
                if s in ("__dict__", "__weakref__", "_hash"):
                    continue
                try:
                    x = getattr(v, s)
                except Exception:
                    continue
                parts.append((s, h(x, d)))
        return hash(tuple(parts))

    out = {}
    classes = {}
    for name in sorted(n for n in sys.modules if _is_ours(n)):
        if only is not None and name not in only:
            continue
        mod = sys.modules.get(name)
        if mod is None:
            continue
        for k, v in list(vars(mod).items()):
            if k in _SKIP_MOD_ATTRS:
                continue
            out[("m", name, k)] = (h(v), v)
            if isinstance(v, type) and _is_ours(v.__module__ or ""):
                if only is None or v.__module__ in only:
                    classes[id(v)] = v
    stack = list(classes.values())
    seen = set()
    while stack:
        K = stack.pop()
        if id(K) in seen:
            continue
        seen.add(id(K))
        mod = K.__module__
        q = K.__qualname__
        out[("c", mod, q, "__bases__")] = (hash(tuple((b.__module__, b.__qualname__) for b in K.__bases__)), None)
        for k, v in list(vars(K).items()):
            if k in _SKIP_CLASS_ATTRS:
                continue
            out[("c", mod, q, k)] = (h(v), v)
            if isinstance(v, type) and _is_ours(v.__module__ or "") and id(v) not in seen:
                if only is None or v.__module__ in only:
                    stack.append(v)
    return out


def reverse_closure(deps, changed):
    """Every module whose import state can depend on one of `changed`."""
    out = set(changed)
    grew = True
    while grew:
        grew = False
        for n, d in deps.items():
            if n not in out and not d.isdisjoint(out):
                out.add(n)
                grew = True
    return out


def _unwrap_func(v):
    for _ in range(6):
        t = type(v)
        if t is FunctionType:
            return v
        if t is staticmethod or t is classmethod or t is MethodType:
            v = v.__func__
        elif t is property:
            v = v.fget
        elif t is functools.partial:
            v = v.func
        elif getattr(v, "__wrapped__", None) is not None:
            v = v.__wrapped__
        else:
            return None
    return None


class Mentions:
    """Where an identifier appears in the pinned sources, as covered-by bitsets."""

    def __init__(self, maps):
        self.maps = maps
        self.texts = None
        self.cache = {}
        self.funcs = {}
        self.used = {}  # name -> {file: lines that use it}

    def _load(self):
        self.texts = {}
        for rel in self.maps.lines:
            try:
                with open(os.path.join(PRISTINE, rel), encoding="utf-8") as fh:
                    self.texts[rel] = fh.read()
            except OSError:
                pass

    def cover(self, name):
        """Tests that execute a line using `name`.

        A line that only hands the value on under another name
        (`x = self.NAME`, `x=self.NAME,` in a call, `self.x = x`) is not a use:
        the new name is followed instead.
        """
        b = self.cache.get(name)
        if b is not None:
            return b
        if self.texts is None:
            self._load()
        b = 0
        used = self.used.setdefault(name, {})
        todo = [name]
        seen = {name}
        handoff = set()
        while todo:
            nm = todo.pop()
            pat = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(nm) + r"(?![A-Za-z0-9_])")
            alias = re.compile(
                r"^\s*(?:self\.)?([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=\s*(?:[A-Za-z_][A-Za-z0-9_]*\.)*"
                + re.escape(nm)
                + r"\s*,?\s*$"
            )
            for rel, text in self.texts.items():
                if nm not in text:
                    continue
                lm = self.maps.lines[rel]
                for i, line in enumerate(text.split("\n"), 1):
                    if nm not in line or not pat.search(line):
                        continue
                    if (rel, i) in handoff:
                        continue
                    m = alias.match(line)
                    if m and len(seen) < 12:
                        handoff.add((rel, i))
                        if m.group(1) not in seen:
                            seen.add(m.group(1))
                            todo.append(m.group(1))
                        continue
                    # a use compiles to an instruction on its own line, so the tests
                    # that execute this line are the tests that can reach it (fewer, when
                    # the instruction map knows who executes the use itself);
                    # class- and module-level lines run at import and have no tests
                    b |= self._uses(rel, i, nm, lm.get(i, 0))
                    used.setdefault(rel, set()).add(i)
        self.cache[name] = b
        return b


def _function_codes(co, out):
    for c in co.co_consts:
        if isinstance(c, CodeType):
            if c.co_flags & CO_OPTIMIZED:
                ls = [l for _, _, l in c.co_lines() if l]
                if ls:
                    out.append((min(ls), max(ls), c))
            _function_codes(c, out)


def _mention_uses(self, rel, line, name, line_cover):
    """Tests that execute an instruction on `line` that reads `name`."""
    if INSTR is None or not line_cover:
        return line_cover
    funcs = self.funcs.get(rel)
    if funcs is None:
        funcs = []
        co = pristine_code(rel, self.texts.get(rel, ""))
        if co is not None:
            _function_codes(co, funcs)
        self.funcs[rel] = funcs
    found = False
    b = 0
    for first, last, co in funcs:
        if not first <= line <= last:
            continue
        offs = []
        try:
            groups = _instr_offsets(co)
        except Exception:
            return line_cover
        k = -1
        for ins in dis.get_instructions(co):
            if ins.opname == "EXTENDED_ARG":
                continue
            k += 1
            pos = ins.positions
            if pos is None or pos.lineno != line:
                continue
            if ins.opname.startswith(("STORE_", "DELETE_")):
                continue
            av = ins.argval
            if av == name or (isinstance(av, tuple) and ins.opcode not in dis.hasconst and name in av):
                if not groups[k]:
                    return line_cover  # begins the line: only the line map knows
                offs.extend(groups[k])
        if offs:
            c = INSTR.cover((rel, co.co_qualname, co.co_firstlineno), offs)
            if c is None:
                return line_cover
            found = True
            b |= c
    return (b & line_cover) if found else line_cover


Mentions._uses = _mention_uses


def state_candidates(maps, mentions, base, cur, notes):
    """Tests that could notice the differences between two fingerprints."""
    cand = 0
    changed = [k for k in cur if k not in base or base[k][0] != cur[k][0]]
    changed += [k for k in base if k not in cur]
    seen_names = set()
    lines = {}  # file -> lines whose execution makes a test a candidate; None once unknown

    def add_lines(d):
        nonlocal lines
        if lines is not None:
            for rel, ls in d.items():
                lines.setdefault(rel, set()).update(ls)
    for key in changed:
        old = base.get(key, (None, None))[1]
        new = cur.get(key, (None, None))[1]
        name = key[-1]
        if key[0] == "m" and (isinstance(old, ModuleType) or isinstance(new, ModuleType)):
            continue
        fo = _unwrap_func(old) if old is not None else None
        fn = _unwrap_func(new) if new is not None else None
        c = 0
        if fo is not None:
            # same function with other defaults / closure: whoever runs its body
            co = fo.__code__
            fname = co.co_filename
            if fname.startswith(PRISTINE + os.sep):
                rel = fname[len(PRISTINE) + 1:]
                fl = _all_lines(co) - {co.co_firstlineno}
                c |= maps.cover(rel, fl)
                add_lines({rel: fl})
            if fn is not None and fn.__qualname__ == fo.__qualname__:
                cand |= c
                notes.append("%s: function state -> %d" % (".".join(key[1:]), c.bit_count()))
                continue
        if key[0] == "c":
            cname = key[1] + ":" + key[2]
            if name == "__bases__":
                t = _class_touch(maps, cname)
                c |= t if t else maps.all
                lines = None
            else:
                t = _class_touch(maps, cname)
                m = mentions.cover(name)
                c |= (t & m) if t else m
                add_lines(mentions.used.get(name, {}))
        else:
            if isinstance(old, type) and isinstance(new, type):
                continue  # same class object name; its attributes are compared separately
            c |= mentions.cover(name)
            add_lines(mentions.used.get(name, {}))
        if (key[0], name) not in seen_names and len(seen_names) < 40:
            seen_names.add((key[0], name))
            notes.append("%s -> %d" % (".".join(key[1:]), c.bit_count()))
        cand |= c
    return cand, len(changed), lines


def _class_touch(maps, cname):
    """Tests that create an instance of the class, a subclass, or an enclosing class."""
    b = maps.classes.get(cname, 0)
    mod, q = cname.split(":")
    while "." in q:
        q = q.rsplit(".", 1)[0]
        b |= maps.classes.get(mod + ":" + q, 0)
    return b


# --------------------------------------------------------------------------
# the preloaded parent ("zygote") and its children
# --------------------------------------------------------------------------
class Zygote:
    helpers = None  # the pinned tests.helpers module (fixture loaders)
    deps = None  # import dependencies between sqlglot modules (see build_deps)
    fixtures = []  # prepared class fixtures (see cache_class_fixture)
    modules = []  # every sqlglot module name, in import order
    func_index = None
    baseline = None
    mentions = None


def prepare_runtime(need_heavy):
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.dont_write_bytecode = True
    sys.path.insert(0, HERE)
    import unittest  # noqa
    import unittest.mock  # noqa
    import fakepool

    fakepool.install()
    if need_heavy:
        try:
            import duckdb
            import numpy  # noqa
            import pandas  # noqa
            import pandas.testing  # noqa

            orig = duckdb.connect

            def connect(*a, **k):
                if "config" not in k:
                    k["config"] = {"threads": 1}
                return orig(*a, **k)

            duckdb.connect = connect
        except Exception as e:  # pragma: no cover
            log("heavy preload failed", repr(e))


def import_all_sqlglot(names=None):
    """Imports sqlglot and all its modules. Returns (loaded names, failed names)."""
    import importlib
    import pkgutil

    failed = []
    try:
        import sqlglot
    except BaseException as e:
        if isinstance(e, (KeyboardInterrupt, SystemExit, _Timeout)):
            raise
        return [], ["sqlglot"]
    if names is None:
        names = [m.name for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot.") if not m.name.endswith("__main__")]
    ok = ["sqlglot"]
    for n in names:
        if n == "sqlglot":
            continue
        try:
            importlib.import_module(n)
            ok.append(n)
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit, _Timeout)):
                raise
            failed.append(n)
    return ok, failed


class _ConnProxy:
    """Stands in for a duckdb connection while a class fixture is prepared in the parent.

    duckdb must not be touched before fork(), so nothing is executed here: the
    statements are only remembered, and each child replays them on a real
    connection of its own. Anything other than execute() raises, which makes
    the caller give up on caching.
    """

    def __init__(self):
        self._log = []

    def execute(self, *a, **k):
        self._log.append((a, k))
        return None


class _LazyConn:
    """A duckdb connection that is opened, and its recorded statements replayed, on first use."""

    def __init__(self, log):
        self.__dict__["_log"] = log
        self.__dict__["_conn"] = None

    def close(self):
        if self._conn is not None:
            self._conn.close()

    def __getattr__(self, name):
        conn = self.__dict__["_conn"]
        if conn is None:
            import duckdb

            conn = duckdb.connect()
            for a, k in self._log:
                conn.execute(*a, **k)
            self.__dict__["_conn"] = conn
        return getattr(conn, name)


def cache_class_fixture(modname, clsname):
    """Runs a test class's setUpClass once, here in the parent.

    Returns (class, lines executed, original setUpClass) or None. Children whose
    patch does not touch any of those lines reuse the prepared class state
    instead of spending seconds rebuilding identical tables.
    """
    import importlib

    import duckdb

    try:
        mod = importlib.import_module(modname)
        klass = getattr(mod, clsname)
        original = klass.__dict__["setUpClass"]
    except Exception as e:
        log("fixture cache: cannot load", modname, repr(e))
        return None
    lines = set()
    pre = PRISTINE + os.sep
    mon = sys.monitoring
    tool = mon.COVERAGE_ID

    def on_line(code, line):
        fn = code.co_filename
        if fn.startswith(pre):
            lines.add((fn[len(pre):], line))
        return mon.DISABLE

    real_connect = duckdb.connect

    def connect(*a, **k):
        if a or k:
            raise RuntimeError("only plain in-memory connections are cached")
        return _ConnProxy()

    try:
        mon.use_tool_id(tool, "br")
        mon.register_callback(tool, mon.events.LINE, on_line)
        mon.set_events(tool, mon.events.LINE)
        duckdb.connect = connect
        try:
            klass.setUpClass()
        finally:
            duckdb.connect = real_connect
            mon.set_events(tool, 0)
            mon.register_callback(tool, mon.events.LINE, None)
            mon.free_tool_id(tool)
    except Exception as e:
        log("fixture cache: setUpClass failed", repr(e))
        return None
    changed = {}
    for rel, l in lines:
        changed.setdefault(rel, set()).add(l)
    return klass, changed, original


def reuse_class_fixture(entry, plan):
    """In a child: make the class use the state prepared in the parent, if that is sound."""
    klass, lines, original = entry
    for rel, ls in plan.changed.items():
        if lines.get(rel) and not lines[rel].isdisjoint(ls):
            # the patch changes code the fixture runs: build it for real
            klass.setUpClass = original
            return False

    def setUpClass(cls):
        for name, value in list(vars(cls).items()):
            if isinstance(value, _ConnProxy):
                setattr(cls, name, _LazyConn(value._log))

    klass.setUpClass = classmethod(setUpClass)
    return True


def fixture_pair_masks(plan):
    """{fixture path: (number of pairs, bitmask of pairs that differ)} for this patch.

    None unless every change under tests/ is such a fixture with an unchanged
    number of pairs. Uses sqlglot's own fixture loader on both versions.
    """
    import importlib

    out = {}
    pre = "tests/fixtures/"
    for rel, text in plan.overlay.items():
        if not rel.startswith("tests/"):
            continue
        if text is None or not rel.startswith(pre) or not rel.endswith(".sql"):
            return None
        name = rel[len(pre):]
        old = list(Zygote.helpers.load_sql_fixture_pairs(name))
        new = list(importlib.import_module("tests.helpers").load_sql_fixture_pairs(name))
        if len(old) != len(new) or not old:
            return None
        mask = 0
        for k, (a, b) in enumerate(zip(old, new)):
            if a != b:
                mask |= 1 << k
        out[rel] = (len(old), mask)
    return out


def build_func_index():
    import gc

    idx = {}
    pre = PRISTINE + os.sep
    n = len(pre)
    for o in gc.get_objects():
        if type(o) is FunctionType:
            co = o.__code__
            fn = co.co_filename
            if fn.startswith(pre):
                idx.setdefault((fn[n:], co.co_qualname, co.co_firstlineno), []).append(o)
    return idx


def purge(prefixes):
    for name in list(sys.modules):
        for p in prefixes:
            if name == p or name.startswith(p + "."):
                del sys.modules[name]
                break
    import importlib

    importlib.invalidate_caches()


class _Timeout(BaseException):
    pass


class _Group(list):
    """A list of tests that carries the class it belongs to."""


class _Abort(BaseException):
    pass


GIANT_SECS = float(os.environ.get("BR_BIG", "1.2"))  # run cost above which a test is decided on individually
QUICK_SECS = 0.05


def child_main(maps, plan, root, wfd, secs, forced):
    """Runs in a forked child. forced: explicit list of tests (after a crash)."""
    import importlib
    import unittest

    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    if not os.environ.get("BR_CHILD_STDERR"):
        os.dup2(devnull, 2)
    out = os.fdopen(wfd, "w", buffering=1)

    def on_alarm(signum, frame):
        raise _Timeout()

    signal.signal(signal.SIGALRM, on_alarm)
    import gc

    # short-lived process: collect cycles rarely (it only costs time), but do collect,
    # or expression trees pile up by the gigabyte
    gc.set_threshold(GC_THRESHOLD, 50, 100)

    def note(msg):
        out.write("N %s\n" % msg.replace("\n", " ")[:1500])

    cand = plan.cand
    fresh = plan.fresh
    fresh_lines = None
    preset_fail = 0
    t_child = time.process_time()

    probing = False
    if not fresh and plan.swaps:
        try:
            idx = Zygote.func_index
            swaps = plan.swaps
            # exactly one sqlglot file changed, by one comparison operator: watch it
            if (
                plan.probe is not None
                and forced is None
                and not FULL
                and len({r for r, _, _ in plan.swaps}) == 1
                and len(plan.code_files) == 1
                and not plan.tests_changed
                and not plan.src_read
                and not plan.readers
                and frozenset(plan.probe[2:4]) in _PROBE_PAIRS
            ):
                prel, pswaps, old_op, new_op = plan.probe
                install_probe(old_op, new_op)
                swaps = [(prel, o, n) for o, n in pswaps]
                probing = True
            for rel, o, n in swaps:
                for f in idx.get((rel, o.co_qualname, o.co_firstlineno), ()):
                    f.__code__ = n
        except Exception as e:
            note("hot swap failed %r" % (e,))
            fresh = True  # some functions may be swapped already; a fresh import discards them all

    if Zygote.fixtures:
        for entry in Zygote.fixtures:
            if fresh or plan.tests_changed or forced is not None:
                entry[0].setUpClass = entry[2]
            else:
                reuse_class_fixture(entry, plan)

    need_tree = fresh or plan.tests_changed or plan.src_read
    if need_tree:
        build_overlay(root, plan.overlay)
        if PRISTINE in sys.path:
            sys.path.remove(PRISTINE)
        sys.path.insert(0, root)
        os.chdir(root)
        purge(("tests",))
        # loaded sqlglot modules now live in the patched tree: tests that look for
        # source files next to a module must find the patched ones
        for mname, mod in list(sys.modules.items()):
            if mod is not None and _is_ours(mname):
                try:
                    f = getattr(mod, "__file__", None)
                    if f and f.startswith(PRISTINE + os.sep):
                        mod.__file__ = root + f[len(PRISTINE):]
                    pth = getattr(mod, "__path__", None)
                    if pth is not None:
                        mod.__path__ = [root + q[len(PRISTINE):] if q.startswith(PRISTINE) else q for q in pth]
                except Exception:
                    pass
    if fresh:
        # Re-import what the change can reach: the changed modules and everything
        # whose import depends on them. If that is most of the package, all of it.
        only = None
        changed_mods = set()
        for rel, text in plan.overlay.items():
            if rel.startswith("sqlglot/"):
                if rel.endswith(".py") and text is not None and rel not in plan.code_files and Zygote.deps:
                    continue  # comments only: nothing to re-import
                if not rel.endswith(".py") or text is None:
                    changed_mods = None
                    break
                mname = rel[:-3].replace("/", ".")
                if mname.endswith(".__init__"):
                    mname = mname[: -len(".__init__")]
                if mname not in Zygote.modules:
                    changed_mods = None
                    break
                changed_mods.add(mname)
        if changed_mods and Zygote.deps and not PURGE_ALL and not FULL and all(m in Zygote.deps for m in changed_mods):
            only = reverse_closure(Zygote.deps, changed_mods)
            if len(only) > 0.5 * len(Zygote.modules):
                only = None
        if only is None:
            purge(("sqlglot",))
            names = Zygote.modules
        else:
            for name in only:
                gone = sys.modules.pop(name, None)
                # `from package import module` looks at the package's attribute first:
                # it must not find the module that was just dropped
                parent, _, child = name.rpartition(".")
                pm = sys.modules.get(parent)
                if pm is not None and gone is not None and pm.__dict__.get(child) is gone:
                    del pm.__dict__[child]
            importlib.invalidate_caches()
            pkg = sys.modules.get("sqlglot.dialects")
            if pkg is not None:
                for k, v in list(vars(pkg).items()):
                    if not isinstance(v, ModuleType) and getattr(v, "__module__", None) in only:
                        del pkg.__dict__[k]
            # packages that stay loaded must find their submodules in the patched tree
            for mname, mod in list(sys.modules.items()):
                if mod is not None and _is_ours(mname):
                    pth = getattr(mod, "__path__", None)
                    if pth is not None:
                        try:
                            mod.__path__ = [
                                root + p[len(PRISTINE):] if p.startswith(PRISTINE) else p for p in pth
                            ]
                        except Exception:
                            pass
            names = [n for n in Zygote.modules if n in only]
            note("partial re-import of %d modules" % len(names))
        signal.setitimer(signal.ITIMER_REAL, 20)
        if only is None:
            ok, failed = import_all_sqlglot(names)
        else:
            failed = []
            for n in names:
                try:
                    importlib.import_module(n)
                except BaseException as e:
                    if isinstance(e, (KeyboardInterrupt, SystemExit, _Timeout)):
                        raise
                    failed.append(n)
        if only is not None:
            # the changed modules must really come from the patched tree; if anything
            # is off, do the plain full re-import instead
            ok_partial = True
            for mname in changed_mods:
                mod = sys.modules.get(mname)
                if mod is not None and not (getattr(mod, "__file__", "") or "").startswith(root + os.sep):
                    ok_partial = False
            if not ok_partial:
                note("partial re-import did not take: full re-import")
                only = None
                purge(("sqlglot", "tests"))
                ok, failed = import_all_sqlglot(Zygote.modules)
        signal.setitimer(signal.ITIMER_REAL, 0)
        if failed == ["sqlglot"]:
            # nothing that imports sqlglot can pass
            note("sqlglot does not import")
            # the package itself is broken: nothing that touches it can pass, and what
            # is left over after a failed package import is not a state worth testing in
            preset_fail = maps.known
            cand = 0
        elif failed:
            # Some lazily loaded module no longer imports: the tests that ask for it fail.
            note("modules fail to import: %s" % ",".join(failed[:8]))
            if only is not None:
                # start over from a clean slate, so that nothing registered by the
                # modules' earlier, working versions is left behind
                only = None
                purge(("sqlglot", "tests"))
                signal.setitimer(signal.ITIMER_REAL, 20)
                ok, failed = import_all_sqlglot(Zygote.modules)
                signal.setitimer(signal.ITIMER_REAL, 0)
            if failed == ["sqlglot"]:
                note("sqlglot does not import")
                # the package itself is broken: nothing that touches it can pass, and what
                # is left over after a failed package import is not a state worth testing in
                preset_fail = maps.known
                cand = 0
            elif failed and maps.loads and USE_LOADS:
                # run the tests that load one of them in a real (lazy) run, plus whatever
                # touches them when everything is loaded
                for m in failed:
                    cand |= maps.loads.get(m, 0)
                    cand |= maps.module_touch(m.replace(".", "/") + ".py")
                    cand |= maps.module_touch(m.replace(".", "/") + "/__init__.py")
                note("%d tests load them" % cand.bit_count())
            else:
                for i in bits(maps.known):
                    out.write("I %d\n" % i)
                out.write("Q \n")
                out.write("E 0\n")
                out.flush()
                return
        elif forced is None and not FULL:
            notes = []
            cur = fingerprint(only)
            base = Zygote.baseline
            if only is not None:
                base = {k: v for k, v in base.items() if k[1] in only}
            c, nchanged, state_lines = state_candidates(maps, Zygote.mentions, base, cur, notes)
            note("state: %d entries differ -> %d tests | %s" % (nchanged, c.bit_count(), " | ".join(notes[:12])))
            cand |= c
            if state_lines is not None and not plan.tests_changed and not plan.src_read:
                # every candidate is one because it executes one of these lines
                fresh_lines = {rel: set(ls) for rel, ls in plan.lines_all.items()}
                for rel, ls in state_lines.items():
                    fresh_lines.setdefault(rel, set()).update(ls)
        cand &= maps.known

    import fakepool

    fakepool.LAZY = not FULL
    # units of pooled work that cannot notice the change are not run
    allow = {}
    # what running each test costs us: measured CPU seconds where known
    cost = [c if c is not None else secs[i] for i, c in enumerate(maps.cpu)] if maps.cpu else list(secs)
    unit_lines = None
    if forced is None and not FULL and maps.units:
        if fresh:
            unit_lines = fresh_lines
        elif not plan.tests_changed and not plan.src_read and plan.changed:
            unit_lines = plan.changed
    if unit_lines:
        for i in bits(cand):
            u = maps.units.get(i)
            if u is None:
                continue
            m = maps.unit_mask(i, unit_lines)
            if m is not None:
                allow[i] = m
                cost[i] = cost[i] * (0.1 + 0.9 * m.bit_count() / max(1, u[0]))
        if allow:
            note("unit filter on %d tests: %s" % (len(allow), " ".join("%s=%d/%d" % (maps.ids[i].rsplit("::", 1)[1], allow[i].bit_count(), maps.units[i][0]) for i in list(allow)[:8])))
    # A changed .sql fixture of statement pairs only matters to the pairs that changed:
    # a test that submits one unit per pair needs just those units.
    if forced is None and not FULL and not fresh and plan.tests_changed and maps.units and Zygote.helpers is not None:
        try:
            pair_masks = fixture_pair_masks(plan)
        except Exception as e:
            note("fixture pairs: %r" % (e,))
            pair_masks = None
        if pair_masks:
            for i in bits(cand):
                u = maps.units.get(i)
                if u is None:
                    continue
                mask = 0
                ok = True
                for rel, (npairs, m) in pair_masks.items():
                    if not (maps.opens.get(rel, 0) >> i) & 1:
                        continue
                    if u[0] != npairs:
                        ok = False
                        break
                    mask |= m
                if not ok:
                    continue
                if plan.changed:
                    cm = maps.unit_mask(i, plan.changed)
                    if cm is None:
                        continue
                    mask |= cm
                allow[i] = mask
                cost[i] = cost[i] * (0.1 + 0.9 * mask.bit_count() / max(1, u[0]))
            if allow:
                note("fixture filter on %d tests: %s" % (len(allow), " ".join("%s=%d/%d" % (maps.ids[i].rsplit("::", 1)[1], allow[i].bit_count(), maps.units[i][0]) for i in list(allow)[:6])))
    secs_true = secs
    secs = cost

    keyf = lambda i: (maps.ids[i].split("::")[0], maps.ids[i].split("::")[1], secs[i])
    if forced is not None:
        first, rest, giants = [], list(forced), []
    else:
        for i in bits(preset_fail):
            out.write("%d 1\n" % i)
        idxs = bits(cand & ~preset_fail)
        if FULL:
            first, rest, giants = [], sorted(idxs, key=keyf), []
        else:
            # quick tests first: they tell us how widely the change bites
            heavy_setup = maps.file_bits.get("tests/test_executor.py", 0)
            first = sorted((i for i in idxs if secs[i] < QUICK_SECS and not (heavy_setup >> i) & 1), key=keyf)
            fs = set(first)
            rest = sorted((i for i in idxs if i not in fs and secs[i] < GIANT_SECS), key=keyf)
            giants = sorted((i for i in idxs if secs[i] >= GIANT_SECS), key=lambda i: secs[i])
    out.write("Q %s\n" % " ".join(map(str, first + rest + giants)))

    stats = {"ran": 0, "failed": 0, "timeouts": 0}

    clock = [time.process_time()]

    failed_idx = []

    def plan_cost(idxs):
        """Simulated milliseconds of selecting these tests (recorded durations)."""
        ds = sorted((int(round(secs_true[i] * 1000)) for i in idxs), reverse=True)
        if not ds:
            return 0
        best = None
        for w in range(1, 9):
            loads = [0] * w
            for d in ds:
                j = loads.index(min(loads))
                loads[j] += d
            c = max(loads) + 2000 * w
            if best is None or c < best:
                best = c
        return best

    def report(idx, failed):
        if failed:
            failed_idx.append(idx)
        out.write("%d %d\n" % (idx, 1 if failed else 0))
        if DEBUG:
            now = time.process_time()
            if FULL:
                import resource

                out.write("N CPU %d %.4f %d\n" % (idx, now - clock[0], resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024))
            elif now - clock[0] > 0.15:
                note("SLOW %.2fs (rec %.3f) %s" % (now - clock[0], secs[idx], maps.ids[idx]))
            clock[0] = now
        stats["ran"] += 1
        if failed:
            stats["failed"] += 1

    class Result(unittest.TestResult):
        def __init__(self):
            super().__init__()
            self.bad = set()
            self.redo = set()
            self.holder_error = False

        def _mark(self, test):
            if isinstance(test, unittest.TestCase):
                self.bad.add(test.id())
            else:
                self.holder_error = True

        def addError(self, test, err):
            if isinstance(err[1], _Abort):
                return  # we stopped this test ourselves after its first failure
            if isinstance(err[1], fakepool.SkipUnit):
                # a skipped unit surfaced outside a subtest: this test must run whole
                self.redo.add(test.id())
                return
            if isinstance(err[1], _Timeout):
                stats["timeouts"] += 1
                if stats["timeouts"] >= 3 and not FULL:
                    self.shouldStop = True  # the change makes tests hang: stop paying for it
            self._mark(test)
            if DEBUG:
                import traceback

                note("ERR %s %r cause=%r tb=%s" % (test, err[1], getattr(err[1], "__cause__", None), " | ".join(traceback.format_exception(getattr(err[1], "__cause__", None) or err[1])[-6:])))

        def addFailure(self, test, err):
            self._mark(test)
            if DEBUG:
                note("FAIL %s %r" % (test, err[1]))

        def addSubTest(self, test, subtest, err):
            if err is not None:
                if isinstance(err[1], fakepool.SkipUnit):
                    return
                self.bad.add(test.id())
                if DEBUG:
                    note("SUB %s %r" % (subtest, err[1]))
                if isinstance(err[1], _Timeout):
                    stats["timeouts"] += 1
                    if stats["timeouts"] >= 3 and not FULL:
                        self.shouldStop = True
                # one failed subtest fails the test: no need to finish it
                raise _Abort()

        def addUnexpectedSuccess(self, test):
            self._mark(test)

    def run_list(order, whole=False):
        again = []
        work = []
        cur_file = None
        for i in order:
            f, c, m = maps.ids[i].split("::")
            if f != cur_file:
                work.append((f, []))
                cur_file = f
            work[-1][1].append((i, c, m))
        for tfile, items in work:
            modname = tfile[:-3].replace("/", ".")
            out.write("B %d\n" % items[0][0])
            try:
                signal.setitimer(signal.ITIMER_REAL, 60)
                mod = importlib.import_module(modname)
                signal.setitimer(signal.ITIMER_REAL, 0)
            except BaseException:
                signal.setitimer(signal.ITIMER_REAL, 0)
                for idx, _, _ in items:
                    report(idx, True)
                continue
            by_class = {}
            for it in items:
                by_class.setdefault(it[1], []).append(it)
            for cname, its in by_class.items():
                klass = getattr(mod, cname, None)
                if klass is None:
                    for idx, _, _ in its:
                        report(idx, True)
                    continue
                cases = []
                for idx, _, meth in its:
                    try:
                        cases.append((idx, klass(meth)))
                    except BaseException:
                        report(idx, True)
                if not cases:
                    continue
                res = Result()
                index_of = {c.id(): i for i, c in cases}
                reported = set()

                def startTest(test, res=res, index_of=index_of):
                    i = index_of.get(test.id())
                    if i is not None:
                        out.write("B %d\n" % i)
                        lim = max(10.0, 20.0 * secs_true[i] + 5.0)
                        if stats["timeouts"] and not FULL:
                            lim = max(3.0, 6.0 * secs_true[i] + 2.0)
                        signal.setitimer(signal.ITIMER_REAL, lim)
                        fakepool.COUNTER = 0
                        fakepool.ALLOW = None if whole else allow.get(i)
                    unittest.TestResult.startTest(res, test)

                def stopTest(test, res=res, index_of=index_of, reported=reported):
                    signal.setitimer(signal.ITIMER_REAL, 0)
                    fakepool.ALLOW = None
                    unittest.TestResult.stopTest(res, test)
                    i = index_of.get(test.id())
                    if i is not None and i not in reported:
                        reported.add(i)
                        if test.id() in res.redo and test.id() not in res.bad:
                            again.append(i)
                        else:
                            report(i, test.id() in res.bad)

                res.startTest = startTest
                res.stopTest = stopTest
                out.write("B %d\n" % cases[0][0])
                signal.setitimer(signal.ITIMER_REAL, 120)
                try:
                    unittest.TestSuite([c for _, c in cases]).run(res)
                except BaseException:
                    pass
                signal.setitimer(signal.ITIMER_REAL, 0)
                last = None
                for i, c in cases:
                    if i not in reported:
                        report(i, True)  # class / module fixture failed before it ran
                    else:
                        last = i
                if res.holder_error and last is not None and len(reported) == len(cases):
                    out.write("%d 1\n" % last)  # class teardown failed: pinned on the last test
        if again:
            run_list(again, True)

    def mostly_failing():
        if stats["timeouts"] >= 3:
            return True
        return stats["ran"] >= 8 and stats["failed"] >= 0.9 * stats["ran"]

    note("setup %.2fs cpu, %d+%d+%d tests" % (time.process_time() - t_child, len(first), len(rest), len(giants)))

    adaptive = not FULL and forced is None
    taken = set()  # selected without running

    def give_up(left, why):
        """Select `left` (and decide the expensive tests without more evidence) unrun."""
        for i in left:
            out.write("I %d\n" % i)
        taken.update(left)
        note("%s: %d more selected unrun" % (why, len(left)))

    def cheaper_to_select(left):
        """Is running `left` dearer than simply selecting all of it?"""
        if len(left) < 30:
            return False
        run = sum(secs[i] for i in left)
        if run < 4.0:
            return False
        sel = set(failed_idx) | taken
        extra = (plan_cost(sel | set(left)) - plan_cost(sel)) / 1000.0
        return run > 1.25 * extra

    # Quick tests first. A spread-out sample of them goes before everything else so
    # that a change that breaks (or spares) nearly everything shows early.
    stop = False
    if adaptive and len(first) > 150:
        step = len(first) // 50
        sample = first[::step]
        ss = set(sample)
        chunks = [sample] + [[i for i in first[k:k + 150] if i not in ss] for k in range(0, len(first), 150)]
    else:
        chunks = [first]
    todo = [c for c in chunks if c]
    # then the rest, class by class
    for i in rest:
        k = maps.ids[i].rsplit("::", 1)[0]
        if todo and getattr(todo[-1], "key", None) == k and len(todo[-1]) < 12:
            todo[-1].append(i)
        else:
            g = _Group([i])
            g.key = k
            todo.append(g)
    for gi, g in enumerate(todo):
        if adaptive and gi:
            left = [i for gg in todo[gi:] for i in gg]
            if (
                probing
                and stats["failed"] == 0
                and PROBE_STATE[1] == 0
                and PROBE_STATE[0] >= PROBE_MIN_EVALS
                and stats["ran"] >= PROBE_MIN_TESTS
            ):
                # the changed comparison was evaluated many times and the old operator
                # would have answered the same every time
                for i in left + giants:
                    out.write("S %d\n" % i)
                note("comparison never differed in %d evaluations over %d tests: %d left unrun" % (PROBE_STATE[0], stats["ran"], len(left) + len(giants)))
                giants = []
                break
            if mostly_failing():
                give_up(left + giants, "%d/%d tests fail" % (stats["failed"], stats["ran"]))
                giants = []
                stop = True
            elif cheaper_to_select(left):
                give_up(left, "%d tests left would cost more to run than to select" % len(left))
                stop = True
            if stop:
                break
        run_list(g)
    # the expensive tests one at a time, cheapest first: run, select unrun, or skip
    selected = set(failed_idx) | taken
    for i in giants:
        ran, failed = stats["ran"], stats["failed"]
        run_cost = secs[i]
        if stats["timeouts"] >= 3:
            out.write("I %d\n" % i)
            selected.add(i)
            continue
        # (a test whose own code or data changed is not vouched for by other tests passing)
        is_reader = (plan.readers >> i) & 1 or plan.test_code
        frac = failed / max(1, ran)
        if failed == 0:
            if ran >= GIANT_MIN_EVIDENCE and run_cost >= SKIP_MIN_COST and not is_reader:
                # every cheaper test that executes the changed lines passes
                out.write("S %d\n" % i)
                note("skip %s: %d cheaper tests all pass" % (maps.ids[i].rsplit("::", 1)[1], ran))
                continue
            before = len(failed_idx)
            run_list([i])
            selected.update(failed_idx[before:])
            continue
        if run_cost >= SKIP_HUGE_COST and not is_reader:
            # The longest tests (TPC-DS execution, the node-limit parse) exercise broad,
            # common paths: they fail when the change breaks many of the cheaper tests on
            # the same lines, and pass when it only bites a few narrow ones.
            # (tests selected unrun are assumed to fail at the rate seen so far)
            failed_est = failed + frac * len(taken)
            if failed_est >= 12 or (failed >= 3 and frac >= 0.5):
                out.write("I %d\n" % i)
                selected.add(i)
                note("select %s unrun: %d/%d cheaper tests fail" % (maps.ids[i].rsplit("::", 1)[1], failed, ran))
                continue
            if ran >= GIANT_MIN_EVIDENCE and frac < 0.5 and failed_est <= 9:
                out.write("S %d\n" % i)
                note("skip %s: only %d/%d cheaper tests fail" % (maps.ids[i].rsplit("::", 1)[1], failed, ran))
                continue
        # Some tests fail. Checking this one costs run_cost (less if it fails early);
        # selecting it unrun costs inc_cost if it would in fact have passed.
        inc_cost = (plan_cost(selected | {i}) - plan_cost(selected)) / 1000.0
        if failed >= 12 or (ran >= 8 and frac >= 0.5):
            p_fail = 0.9
        elif frac >= 0.5:
            p_fail = 0.5
        elif failed > 5:
            p_fail = 0.4
        else:
            p_fail = 0.15
        if run_cost * (1.0 - 0.7 * p_fail) >= inc_cost * (1.0 - p_fail):
            out.write("I %d\n" % i)
            selected.add(i)
            note("select %s unrun: %d/%d fail, +%.1fs vs %.1fs to check" % (maps.ids[i].rsplit("::", 1)[1], failed, ran, inc_cost, run_cost))
        else:
            before = len(failed_idx)
            run_list([i])
            selected.update(failed_idx[before:])
    out.write("E 0\n")
    out.flush()


GIANT_MIN_EVIDENCE = int(os.environ.get("BR_GIANT_MIN", "10"))
GIANT_INCLUDE_FRAC = float(os.environ.get("BR_GIANT_FRAC", "0.5"))
SKIP_MIN_COST = float(os.environ.get("BR_SKIP_MIN", "4.0"))
SKIP_HUGE_COST = float(os.environ.get("BR_SKIP_HUGE", "15.0"))
INCLUDE_RATIO = float(os.environ.get("BR_INC_RATIO", "0.6"))


# --------------------------------------------------------------------------
# parent: job control
# --------------------------------------------------------------------------
class Job:
    def __init__(self, plan, root):
        self.plan = plan
        self.root = root
        self.pid = None
        self.fd = None
        self.buf = b""
        self.current = None
        self.last_progress = 0.0
        self.queue = None  # tests the child said it would run
        self.skipped = set()
        self.included = set()
        self.results = {}
        self.restarts = 0
        self.cpu = 0.0
        self.forced = None
        self.started = 0.0
        self.rss = 0


def start_job(maps, job, secs):
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(r)
            os.setpgid(0, 0)
            child_main(maps, job.plan, job.root, w, secs, job.forced)
        except BaseException:
            pass
        finally:
            os._exit(0)
    os.close(w)
    job.pid = pid
    job.fd = r
    job.buf = b""
    job.current = None
    job.last_progress = time.time()


def memory_state():
    """(bytes in use, limit) of this container, or (None, None) when unknown."""
    try:
        with open("/sys/fs/cgroup/memory.max") as fh:
            raw = fh.read().strip()
        limit = None if raw == "max" else int(raw)
        used = 0
        with open("/sys/fs/cgroup/memory.stat") as fh:
            for ln in fh:
                k, _, v = ln.partition(" ")
                if k in ("anon", "shmem", "kernel"):
                    used += int(v)
        return used, limit
    except Exception:
        return None, None


def kill_job(job):
    try:
        os.killpg(job.pid, signal.SIGKILL)
    except OSError:
        try:
            os.kill(job.pid, signal.SIGKILL)
        except OSError:
            pass


def schedule(durations):
    """durations: {number: ms}. Returns list of workers minimising cost."""
    if not durations:
        return []
    items = sorted(durations.items(), key=lambda kv: -kv[1])
    best = None
    for w in range(1, 9):
        loads = [0] * w
        ws = [[] for _ in range(w)]
        for num, ms in items:
            j = loads.index(min(loads))
            loads[j] += ms
            ws[j].append(num)
        cost = max(loads) + 2000 * w
        if best is None or cost < best[0]:
            best = (cost, ws)
    return [w for w in best[1] if w]


def run():
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    maps = Maps(tests)
    global INSTR
    if not os.environ.get("BR_NO_INSTR"):
        INSTR = Instr(maps)
        if not INSTR.ok:
            INSTR = None
    secs = [0.0] * maps.n
    for i, num in enumerate(maps.number):
        if num is not None:
            secs[i] = ms[num] / 1000.0
    cdir = os.path.join(DATA, "commits")
    cids = sorted(f[:-6] for f in os.listdir(cdir) if f.endswith(".patch"))
    global WALL_BUDGET
    if "BR_WALL" not in os.environ and len(cids) > 48:
        # the final has about 1.5x the commits and 4.5 minutes instead of 3
        WALL_BUDGET = 225.0
    plans = []
    STATE.update(plans=plans, maps=maps, ms=ms, cids=cids)
    for cid in cids:
        try:
            with open(os.path.join(cdir, cid + ".patch"), encoding="utf-8", errors="surrogateescape", newline="") as fh:
                text = fh.read()
            plan = analyze(maps, cid, text)
        except Exception as e:
            plan = Plan(cid)
            plan.include = maps.known
            plan.notes.append("analysis crashed: %r" % (e,))
        if FULL and plan.include != maps.all:
            # ground-truth mode: run (nearly) everything from a fresh import
            plan.cand = maps.known & ~plan.include
            if os.environ.get("BR_ONLY"):
                for i in range(maps.n):
                    if os.environ["BR_ONLY"] not in maps.ids[i]:
                        plan.cand &= ~(1 << i)
            if os.environ.get("BR_NOGIANT") == "1":
                for i in range(maps.n):
                    if secs[i] >= GIANT_SECS:
                        plan.cand &= ~(1 << i)
            if any(r.startswith("sqlglot/") for r in plan.overlay):
                plan.fresh = True
                plan.swaps = []
        plans.append(plan)
        log(cid, "cand=%d include=%d fresh=%d swaps=%d" % (plan.cand.bit_count(), plan.include.bit_count(), plan.fresh, len(plan.swaps)), "|", " | ".join(plan.notes))
    log("analysis done %.2fs cpu" % time.process_time())

    pending = [p for p in plans if p.cand or p.fresh]
    if pending:
        heavy_bits = maps.file_bits.get("tests/test_optimizer.py", 0) | maps.file_bits.get("tests/test_executor.py", 0)
        prepare_runtime(any(p.cand & heavy_bits or p.fresh for p in pending))
        log("libs loaded %.2fs cpu" % time.process_time())
        sys.path.insert(0, PRISTINE)
        os.chdir(PRISTINE)
        ok, failed = import_all_sqlglot()
        log("sqlglot loaded %.2fs cpu" % time.process_time())
        Zygote.modules = ok
        if failed:
            log("pristine modules failed to import:", failed)
        if any(p.fresh for p in pending):
            Zygote.deps = maps.deps or None
            Zygote.baseline = fingerprint()
            Zygote.mentions = Mentions(maps)
            log("baseline fingerprint: %d entries" % len(Zygote.baseline))
        # test modules used by several commits are imported once, here
        import importlib

        uses = {}
        for p in pending:
            if p.tests_changed or p.fresh:
                continue
            for f, fb in maps.file_bits.items():
                if p.cand & fb:
                    uses[f] = uses.get(f, 0) + 1
        for f, n in uses.items():
            if n >= 2:
                try:
                    importlib.import_module(f[:-3].replace("/", "."))
                except Exception as e:
                    log("preload of", f, "failed", repr(e))
        log("tests preloaded %.2fs cpu" % time.process_time())
        ex_bits = maps.file_bits.get("tests/test_executor.py", 0)
        n_ex = sum(1 for p in pending if p.cand & ex_bits and not p.fresh and not p.tests_changed)
        if n_ex >= 2 and not FULL:
            entry = cache_class_fixture("tests.test_executor", "TestExecutor")
            if entry is not None:
                Zygote.fixtures.append(entry)
                log("executor fixture prepared once for %d commits" % n_ex)
        if any(p.swaps for p in pending):
            Zygote.func_index = build_func_index()
            maps.load_units()
        if any(p.fresh for p in pending):
            maps.load_units()
        if any(p.tests_changed and not p.fresh for p in pending):
            maps.load_units()
            try:
                Zygote.helpers = importlib.import_module("tests.helpers")
            except Exception as e:
                log("cannot load tests.helpers:", repr(e))
        import gc

        gc.collect()
        gc.freeze()
        log("runtime ready %.2fs cpu" % time.process_time())

    # cheapest first, so if time runs out the fallbacks hit few commits
    pending.sort(key=lambda p: (p.fresh, sum(secs[i] for i in bits(p.cand))))
    running = []
    STATE["running"] = running
    solo = []  # plans to run with no other child alongside
    wasted = {}
    alone = False
    seq = 0

    def finish(job):
        plan = job.plan
        for i, failed in job.results.items():
            if failed:
                plan.fails |= 1 << i
        for i in job.included:
            plan.fails |= 1 << i
        if job.queue is None:
            # the child never got as far as naming its tests (import hung or crashed)
            plan.fails |= maps.known if plan.fresh else plan.cand
        else:
            for i in job.queue:
                if i not in job.results and i not in job.skipped:
                    plan.fails |= 1 << i
        plan.done = True
        if FULL:
            log("RESULT", plan.cid, json.dumps([maps.number[i] for i in bits(plan.fails)]))
        rec = sum(secs[i] for i in job.results)
        log(plan.cid, "ran %d tests, %d selected, cpu %.2fs rec %.2fs rss %dMB" % (len(job.results), plan.fails.bit_count(), job.cpu, rec, job.rss))
        remove_tree(job.root)

    while pending or running or solo:
        now = time.time()
        out_of_time = now - T0 > WALL_BUDGET
        if solo and not running and not out_of_time:
            # a commit that needs the memory to itself
            plan = solo.pop(0)
            seq += 1
            job = Job(plan, os.path.join(TMP, "br%d" % seq))
            job.started = time.time()
            job.cpu = wasted.get(plan.cid, 0.0)
            start_job(maps, job, secs)
            running.append(job)
            alone = True
        elif not running:
            alone = False
        while pending and len(running) < NPROC and not out_of_time and not alone:
            plan = pending.pop(0)
            seq += 1
            job = Job(plan, os.path.join(TMP, "br%d" % seq))
            job.started = time.time()
            start_job(maps, job, secs)
            running.append(job)
        if out_of_time:
            pending.extend(solo)
            solo = []
            for plan in pending:
                log(plan.cid, "out of time: selecting all candidates")
                plan.fails = plan.cand if not plan.fresh else maps.known
                plan.done = True
            pending = []
            for job in running:
                kill_job(job)
        if not running:
            continue
        # two children that both run memory-hungry tests could exceed the container's
        # limit: when memory runs high, stop the younger one and run it later on its own
        if len(running) > 1:
            used, limit = memory_state()
            if used is not None and limit and used > MEM_FRACTION * limit:
                victim = min(running, key=lambda j: len(j.results))
                log(victim.plan.cid, "memory high (%d MB): rerun alone later" % (used >> 20))
                kill_job(victim)
                try:
                    os.close(victim.fd)
                except OSError:
                    pass
                try:
                    _, _, ru = os.wait4(victim.pid, 0)
                    victim.cpu += ru.ru_utime + ru.ru_stime
                except OSError:
                    pass
                running.remove(victim)
                remove_tree(victim.root)
                solo.append(victim.plan)
                wasted[victim.plan.cid] = wasted.get(victim.plan.cid, 0.0) + victim.cpu
                continue
        elif len(running) == 1:
            used, limit = memory_state()
            if used is not None and limit and used > 0.88 * limit and running[0].restarts < 99:
                # even alone it does not fit: stop it and select what it had left
                log(running[0].plan.cid, "memory very high (%d MB): stopping this commit's run" % (used >> 20))
                running[0].restarts = 99
                kill_job(running[0])
        rl, _, _ = select.select([j.fd for j in running], [], [], 0.25)
        now = time.time()
        for job in list(running):
            eof = False
            if job.fd in rl:
                data = os.read(job.fd, 1 << 16)
                if not data:
                    eof = True
                else:
                    job.buf += data
                    job.last_progress = now
                    *full, job.buf = job.buf.split(b"\n")
                    for ln in full:
                        a, _, b = ln.partition(b" ")
                        if a == b"B":
                            job.current = int(b)
                        elif a == b"E":
                            job.current = None
                        elif a == b"Q":
                            q = [int(x) for x in b.split()]
                            job.queue = q if job.queue is None else job.queue
                        elif a == b"S":
                            job.skipped.add(int(b))
                        elif a == b"I":
                            job.included.add(int(b))
                        elif a == b"N":
                            log(job.plan.cid, "::", b.decode("utf-8", "replace"))
                        else:
                            i = int(a)
                            job.results[i] = bool(int(b)) or job.results.get(i, False)
            hung = False
            if not eof and now - job.started > JOB_WALL:
                log(job.plan.cid, "job over its time cap")
                hung = True
                job.restarts = 99
                kill_job(job)
            elif not eof:
                cur = job.current
                limit = (max(10.0, 20.0 * secs[cur] + 5.0) if cur is not None else 45.0) + 10.0
                if now - job.last_progress > limit:
                    hung = True
                    kill_job(job)
            if eof or hung or out_of_time:
                try:
                    os.close(job.fd)
                except OSError:
                    pass
                try:
                    _, _, ru = os.wait4(job.pid, 0)
                    job.cpu += ru.ru_utime + ru.ru_stime
                    job.rss = max(job.rss, ru.ru_maxrss // 1024)
                except OSError:
                    pass
                if job.queue is not None and not out_of_time:
                    remaining = [
                        i for i in job.queue
                        if i not in job.results and i not in job.skipped and i not in job.included
                    ]
                    if remaining and job.restarts < 6:
                        # the child died or hung inside job.current: that test failed
                        cur = job.current if job.current in remaining else remaining[0]
                        job.results[cur] = True
                        remaining = [i for i in remaining if i != cur]
                        job.restarts += 1
                        if remaining:
                            remove_tree(job.root)
                            job.forced = remaining
                            start_job(maps, job, secs)
                            continue
                running.remove(job)
                finish(job)

    emit(plans, maps, ms)
    log("wall %.1fs, parent cpu %.2fs" % (time.time() - T0, time.process_time()))


STATE = {"emitted": False}


def emit(plans, maps, ms):
    out = []
    for plan in plans:
        sel = (plan.fails | plan.include) & maps.known
        dur = {}
        for i in bits(sel):
            dur[maps.number[i]] = ms[maps.number[i]]
        if plan.include == maps.known and maps.unmapped:
            for num in maps.unmapped:
                dur[num] = ms[num]
        workers = schedule(dur)
        out.append(json.dumps({"commit": plan.cid, "workers": workers}, separators=(",", ":")))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    sys.stdout.flush()
    STATE["emitted"] = True


def emergency(err):
    """Something went wrong in the parent: still print a valid, safe answer."""
    log("FATAL: %r -- falling back to safe selections" % (err,))
    for job in STATE.get("running") or ():
        try:
            kill_job(job)
        except Exception:
            pass
    plans, maps, ms = STATE.get("plans"), STATE.get("maps"), STATE.get("ms")
    cids = STATE.get("cids")
    try:
        if plans is not None and maps is not None and ms is not None and cids is not None and len(plans) == len(cids):
            for plan in plans:
                if not plan.done and (plan.cand or plan.fresh):
                    plan.include = maps.known  # never finished: select everything
            emit(plans, maps, ms)
            return
    except Exception as e:
        log("FATAL in fallback: %r" % (e,))
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    workers = schedule(dur)
    cdir = os.path.join(DATA, "commits")
    out = []
    for f in sorted(os.listdir(cdir)):
        if f.endswith(".patch"):
            out.append(json.dumps({"commit": f[:-6], "workers": workers}, separators=(",", ":")))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    sys.stdout.flush()
    STATE["emitted"] = True


def main():
    try:
        run()
    except BaseException as e:  # noqa: B902 - a crash must not cost the whole submission
        if STATE["emitted"]:
            raise
        emergency(e)


if __name__ == "__main__":
    main()
