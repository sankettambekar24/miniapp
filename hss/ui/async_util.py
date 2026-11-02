"""Run blocking calls (backend HTTP, file IO) off the GUI thread and deliver results on the GUI thread."""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class _Sig(QObject):
    done = Signal(int, object, object)  # token, result, error


class _Task(QRunnable):
    def __init__(self, token: int, fn: Callable, sig: _Sig):
        super().__init__()
        self.token, self.fn, self.sig = token, fn, sig

    def run(self) -> None:
        try:
            self.sig.done.emit(self.token, self.fn(), None)
        except Exception as exc:  # delivered to the error callback; never raised in the pool
            self.sig.done.emit(self.token, None, exc)


class Runner(QObject):
    """`run(fn, ok, err)`: fn runs in a pool thread; ok(result)/err(exception) run on the GUI thread."""

    def __init__(self) -> None:
        super().__init__()
        self._sig = _Sig()
        self._sig.done.connect(self._on_done)
        self._cbs: dict[int, tuple[Callable | None, Callable | None]] = {}
        self._n = 0
        self._pool = QThreadPool.globalInstance()

    def run(self, fn: Callable, ok: Callable | None = None, err: Callable | None = None) -> None:
        self._n += 1
        self._cbs[self._n] = (ok, err)
        self._pool.start(_Task(self._n, fn, self._sig))

    def _on_done(self, token: int, result, error) -> None:
        ok, err = self._cbs.pop(token, (None, None))
        if error is not None:
            if err:
                err(error)
        elif ok:
            ok(result)
