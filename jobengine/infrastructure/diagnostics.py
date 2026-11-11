"""Opt-in, context-local observations of the production workflow."""

from contextlib import contextmanager
from contextvars import ContextVar

_observer = ContextVar("jobengine_observer", default=None)
_job = ContextVar("jobengine_diagnostic_job", default=None)


def observing():
    return _observer.get() is not None


def emit(stage, outcome, **details):
    observer = _observer.get()
    if observer is not None:
        observer(stage, outcome, job=_job.get(), **details)


@contextmanager
def observe(callback):
    token = _observer.set(callback)
    try:
        yield
    finally:
        _observer.reset(token)


@contextmanager
def job_context(item):
    token = _job.set(item.model_dump(mode="json"))
    try:
        yield
    finally:
        _job.reset(token)
