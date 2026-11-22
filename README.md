# Blast Radius selector

Picks, for each patch against sqlglot `eb11aec`, the tests that fail on it.

Nothing is looked up. Every answer comes from **running the pinned tests
against the patched code** inside the container; maps recorded on the pinned
commit only decide which tests are worth running.

## How it works

For each patch (`selector.py`):

1. **Apply the patch in memory** and compile the changed Python files. Old and
   new code objects are compared function by function, ignoring line numbers,
   so comments and formatting are recognised as no-ops and a real change is
   pinned to the exact instructions that differ.

2. **Find candidate tests** from maps built once on the pinned commit
   (`maps.bin`, `units.bin`, `instr.bin`; see `tools/`):
   - which bytecode instructions and source lines of sqlglot each test
     executes, recorded with every test in a fresh process so warm caches
     cannot hide a dependency;
   - which sqlglot classes each test instantiates;
   - which repository files (fixtures, sources) each test reads;
   - for tests that farm work out to a process pool (optimizer fixtures,
     TPC-H/TPC-DS execution), the coverage of every unit of work separately;
   - which modules each sqlglot module's import depends on, and which lazily
     loaded modules each test pulls in.

   A changed instruction inside a function selects the tests that execute it.
   A changed fixture selects the tests that read it.

3. **Run the candidates against the patched code**, in forked children of one
   process that has sqlglot and the test modules loaded already.
   - A change confined to function bodies is applied by swapping the new code
     objects into the loaded functions.
   - A change to anything evaluated at import time (class attributes, module
     constants, functions that run during import) re-imports the affected
     modules from the patched tree and diffs a fingerprint of every module
     global and class attribute against the pinned one. Tests that create an
     instance of a class and execute an instruction reading a changed
     attribute of it are run.
   - Tests run in-process through `unittest`, with the process pool replaced
     by a stand-in (`fakepool.py`) that keeps the pickle round trip. Units of
     pooled work that never execute a changed line are skipped; for a changed
     fixture of statement pairs, only the changed pairs' units run.
   - A test stops at its first failed subtest.

4. **Print the tests that failed**, packed onto 1-8 workers to minimise
   `busiest worker + 2 s per worker`.

Some decisions are made without running a test, always from what was just
measured on this patch:

- If nearly every quick candidate fails, the rest are selected unrun; so is a
  broad set of candidates when running it would cost more CPU than selecting
  it adds in simulated time.
- The few very long tests are skipped when all cheaper tests that execute the
  same changed instructions pass, and selected unrun when most of those fail.
- When the patch only swaps `is`/`==` (or `is not`/`!=`) in one comparison,
  that comparison is instrumented to evaluate both operators on the same
  operands. If they never disagree over hundreds of evaluations in the first
  tests, the remaining tests are not run: they execute exactly as on the pinned
  commit.

Anything the analysis cannot handle (unparsable patch, hunks that do not
apply, binary files) falls back to selecting more tests, never fewer. When a
module stops importing, the tests that load it are run against the broken
tree. A watchdog keeps the children inside the memory and time limits, and if
the selector itself ever fails it still prints a valid, conservative answer.

## Layout

| | |
| --- | --- |
| `Dockerfile` | labeled base image, the locked packages, sqlglot at the pinned commit |
| `run.py`, `selector.py` | everything that runs at scoring time |
| `fakepool.py` | in-process stand-in for `ProcessPoolExecutor` |
| `maps.bin`, `units.bin`, `instr.bin` | maps recorded on the pinned commit |
| `tools/mapgen.py`, `tools/depgen.py`, `tools/loadgen.py`, `tools/compact.py` | how the maps were built |
| `tools/cpu.json`, `tools/deps.json` | measured per-test CPU, recorded import dependencies |
| `fetch_sqlglot.py` | build step: download sqlglot at the pinned commit |

## Rebuilding the maps

```sh
# inside the image (sqlglot at /opt/sqlglot), with tests.json from the kit
for i in 0 1 2 3 4 5 6 7; do python tools/mapgen.py /opt/sqlglot tests.json raw/s$i.pickle $i 8 & done; wait
for i in 0 1 2 3 4 5 6 7; do python tools/loadgen.py /opt/sqlglot tests.json raw/loads$i.json $i 8 & done; wait
python tools/depgen.py /opt/sqlglot tools/deps.json
python tools/compact.py raw tests.json maps.bin units.bin instr.bin
```
