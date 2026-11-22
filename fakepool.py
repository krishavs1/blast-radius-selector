"""In-process stand-in for ProcessPoolExecutor.

sqlglot's optimizer and executor tests push work to spawned worker
processes. Running that work in-process (with the same pickle round trip the
real pool does) keeps it visible to tracing and avoids paying interpreter
start-up for every worker.
"""
import concurrent.futures as cf
from concurrent.futures import _base
import pickle
from concurrent.futures.process import BrokenProcessPool


class SkipUnit(Exception):
    """Raised in place of the result of a unit of work that was not run."""


# When set, only the submissions whose running index has its bit set are run:
# the others cannot be affected by the change under test (they never execute a
# changed line), so their outcome is known to be the same as on the pinned commit.
ALLOW = None
COUNTER = 0
SKIPPED = 0


# When True a unit runs when its result is first asked for, not when it is
# submitted, so a test that is stopped at its first failure never pays for the
# units it did not get to.
LAZY = False


def _compute(f, fn, args, kwargs):
    try:
        res = fn(*args, **kwargs)
        res = pickle.loads(pickle.dumps(res))
    except BaseException as e:
        if isinstance(e, (KeyboardInterrupt, SystemExit)) or type(e).__name__ in ("_Timeout", "_Abort"):
            raise
        f.set_exception(e)
    else:
        f.set_result(res)


class LazyFuture(cf.Future):
    _work = None

    def _force(self):
        w = self._work
        if w is not None:
            self._work = None
            _compute(self, *w)

    def result(self, timeout=None):
        self._force()
        return super().result(timeout)

    def exception(self, timeout=None):
        self._force()
        return super().exception(timeout)


_real_wait = cf.wait
_real_as_completed = cf.as_completed


def _wait(fs, timeout=None, return_when=cf.ALL_COMPLETED):
    fs = list(fs)
    if any(isinstance(f, LazyFuture) for f in fs):
        return _base.DoneAndNotDoneFutures(set(fs), set())
    return _real_wait(fs, timeout, return_when)


def _as_completed(fs, timeout=None):
    fs = list(fs)
    if any(isinstance(f, LazyFuture) for f in fs):
        return iter(fs)
    return _real_as_completed(fs, timeout)


class SyncPool:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        self._broken = None
        if initializer is not None:
            try:
                initializer(*initargs)
            except BaseException as e:  # a real pool reports this as a broken pool
                if isinstance(e, (KeyboardInterrupt, SystemExit)):
                    raise
                self._broken = e

    def submit(self, fn, /, *args, **kwargs):
        global COUNTER, SKIPPED
        f = LazyFuture()
        if self._broken is not None:
            f.set_exception(BrokenProcessPool("initializer failed"))
            return f
        k = COUNTER
        COUNTER += 1
        if ALLOW is not None and not (ALLOW >> k) & 1:
            SKIPPED += 1
            if getattr(fn, "__name__", "") == "mp_execute":
                f.set_result(None)  # the executor tests read None as "nothing to compare"
            else:
                f.set_exception(SkipUnit())
            return f
        try:
            fn2, args2, kwargs2 = pickle.loads(pickle.dumps((fn, args, kwargs)))
        except BaseException as e:
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            f.set_exception(e)
            return f
        if LAZY:
            f._work = (fn2, args2, kwargs2)
        else:
            _compute(f, fn2, args2, kwargs2)
        return f

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        futs = [self.submit(fn, *a) for a in zip(*iterables)]
        return (f.result() for f in futs)

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def install():
    import concurrent.futures.process as p

    cf.ProcessPoolExecutor = SyncPool
    p.ProcessPoolExecutor = SyncPool
    cf.wait = _wait
    cf.as_completed = _as_completed
