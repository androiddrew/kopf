"""
Synchronisation primitives and helper functions.
"""
import asyncio
import collections.abc
import enum
import threading
import time
from typing import Awaitable, Collection, Generator, Generic, Optional, TypeVar, Union

from kopf.aiokits import aiotasks


async def condition_chain(
        source: asyncio.Condition,
        target: asyncio.Condition,
) -> None:
    """
    A condition chain is a "clean" hack to attach one condition to another.

    It is a "clean" (not "dirty") hack to wake up the webhook configuration
    managers when either the resources are revised (as seen in the insights),
    or a new client config is yielded from the webhook server.
    """
    async with source:
        while True:
            await source.wait()
            async with target:
                target.notify_all()


FlagReasonT = TypeVar('FlagReasonT', bound=enum.Flag)


class FlagSetter(Generic[FlagReasonT]):
    """
    A boolean flag indicating that the daemon should stop and exit.

    Every daemon gets a ``stopped`` kwarg, which is an event-like object.
    The stopped flag is raised in two cases:

    * The corresponding k8s object is deleted, so the daemon should stop.
    * The whole operator is stopping, so all the daemons should stop too.

    The stopped flag is a graceful way of a daemon termination.
    If the daemons do not react to their stoppers and continue running,
    their tasks are cancelled by raising a `asyncio.CancelledError`.

    .. warning::
        In case of synchronous handlers, which are executed in the threads,
        this can lead to the OS resource leakage:
        there is no way to kill a thread in Python, so it will continue running
        forever or until failed (e.g. on an API call for an absent resource).
        The orphan threads will block the operator's process from exiting,
        thus affecting the speed of restarts.
    """

    def __init__(self) -> None:
        super().__init__()
        self.when: Optional[float] = None
        self.reason: Optional[FlagReasonT] = None
        self.sync_event = threading.Event()
        self.async_event = asyncio.Event()
        self.sync_waiter: SyncFlagWaiter[FlagReasonT] = SyncFlagWaiter(self)
        self.async_waiter: AsyncFlagWaiter[FlagReasonT] = AsyncFlagWaiter(self)

    def __repr__(self) -> str:
        return f'<{self.__class__.__name__}: {self.is_set()}, reason={self.reason}>'

    def is_set(self, reason: Optional[FlagReasonT] = None) -> bool:
        """
        Check if the daemon stopper is set: at all or for a specific reason.
        """
        matching_reason = reason is None or (self.reason is not None and reason in self.reason)
        return matching_reason and self.sync_event.is_set()

    def set(self, reason: Optional[FlagReasonT] = None) -> None:
        reason = reason if reason is not None else self.reason  # to keep existing values
        self.when = self.when if self.when is not None else time.monotonic()
        self.reason = reason if self.reason is None or reason is None else self.reason | reason
        self.sync_event.set()
        self.async_event.set()  # it is thread-safe: always called in operator's event loop.


class FlagWaiter(Generic[FlagReasonT]):
    """
    A minimalistic read-only checker for the daemons from the user side.

    This object is fed into the :kwarg:`stopped` kwarg for the handlers.

    The flag setter is hidden from the users, and is an internal class.
    The users should not be able to trigger the stopping activities.

    Usage::

        @kopf.daemon('kopfexamples')
        def handler(stopped, **kwargs):
            while not stopped:
                ...
                stopped.wait(60)
    """

    def __init__(self, setter: FlagSetter[FlagReasonT]) -> None:
        super().__init__()
        self._setter = setter

    def __repr__(self) -> str:
        return repr(self._setter)

    def __bool__(self) -> bool:
        return self._setter.is_set()

    def is_set(self) -> bool:
        return self._setter.is_set()

    @property
    def reason(self) -> Optional[FlagReasonT]:
        return self._setter.reason

    # See the docstring for AsyncFlagPromise for explanation.
    def wait(self, timeout: Optional[float] = None) -> "FlagWaiter[FlagReasonT]":
        # Presumably, `await stopped.wait(n).wait(m)` in async mode.
        raise NotImplementedError("Please report the use-case in the issue tracker if needed.")

    # See the docstring for AsyncFlagPromise for explanation.
    def __await__(self) -> Generator[None, None, "FlagWaiter[FlagReasonT]"]:
        # Presumably, `await stopped` in either sync or async mode.
        raise NotImplementedError("Daemon stoppers should not be awaited directly. "
                                  "Use `await stopped.wait()`.")


class SyncFlagWaiter(FlagWaiter[FlagReasonT], Generic[FlagReasonT]):
    def wait(self, timeout: Optional[float] = None) -> "SyncFlagWaiter[FlagReasonT]":
        self._setter.sync_event.wait(timeout=timeout)
        return self


class AsyncFlagWaiter(FlagWaiter[FlagReasonT], Generic[FlagReasonT]):
    def wait(self, timeout: Optional[float] = None) -> "AsyncFlagPromise[FlagReasonT]":
        # A new checker instance, which is awaitable and returns the original checker in the end.
        return AsyncFlagPromise(self, timeout=timeout)


class AsyncFlagPromise(FlagWaiter[FlagReasonT],
                       Awaitable[AsyncFlagWaiter[FlagReasonT]],
                       Generic[FlagReasonT]):
    """
    An artificial future-like promise for ``await stopped.wait(...)``.

    This is a low-level "clean hack" to simplify the publicly faced types.
    As a trade-off, the complexity moves to the implementation side.

    The only goal is to accept one and only one type for the ``stopped`` kwarg
    in ``@kopf.daemon()`` handlers (protocol `callbacks.ResourceDaemonFn`)
    regardless of whether they are sync or async, but still usable for both.

    For this, the kwarg is announced with the base type `DaemonStoppingFlag`,
    not as a union of sync or async, or any other double-typed approach.

    The challenge is to support ``stopped.wait()`` in both sync & async modes:

    * sync: ``stopped.wait(float) -> bool``.
    * async: ``await stopped.wait(float) -> bool``.

    The sync case is the primary case as there are no alternatives to it.
    The async case is secondary because such use is discouraged (but supported);
    it is recommended to rely on regular task cancellations in async daemons.

    To solve it, instead of returning a ``bool``, a ``bool``-evaluable object
    is returned --- the checker itself. This solves the primary (sync) use-case:
    just sleep for the requested duration and return bool-evaluable self.

    To follow the established signatures of the primary (sync) use-case,
    the secondary (async) use-case also returns an instance of the checker:
    but not "self"! Instead, it creates a new time-limited & awaitable checker
    for every call to ``stopped.wait(n)``, limiting its life by ``n`` seconds.

    Extra checker instances add some tiny memory overhead, but this is fine
    since the use-case is discouraged and there is a better native alternative.

    Then, going back through the class hierarchy, all classes are made awaitable
    so that this functionality becomes exposed via the declared base class.
    But all checkers except the time-limited one prohibit waiting for them.
    """

    def __init__(self, waiter: AsyncFlagWaiter[FlagReasonT], *, timeout: Optional[float]) -> None:
        super().__init__(waiter._setter)
        self._timeout = timeout
        self._waiter = waiter

    def __await__(self) -> Generator[None, None, AsyncFlagWaiter[FlagReasonT]]:
        name = f"time-limited waiting for the daemon stopper {self._setter!r}"
        coro = asyncio.wait_for(self._setter.async_event.wait(), timeout=self._timeout)
        task = aiotasks.create_task(coro, name=name)
        try:
            yield from task
        except asyncio.TimeoutError:
            pass
        return self._waiter  # the original checker! not the time-limited one!


class DaemonStoppingReason(enum.Flag):
    """
    A reason or reasons of daemon being terminated.

    Daemons are signalled to exit usually for two reasons: the operator itself
    is exiting or restarting, so all daemons of all resources must stop;
    or the individual resource was deleted, but the operator continues running.

    No matter the reason, the daemons must exit, so one and only one stop-flag
    is used. Some daemons can check the reason of exiting if it is important.

    There can be multiple reasons combined (in rare cases, all of them).
    """
    DONE = enum.auto()  # whatever the reason and the status, the asyncio task has exited.
    FILTERS_MISMATCH = enum.auto()  # the resource does not match the filters anymore.
    RESOURCE_DELETED = enum.auto()  # the resource was deleted, the asyncio task is still awaited.
    OPERATOR_PAUSING = enum.auto()  # the operator is pausing, the asyncio task is still awaited.
    OPERATOR_EXITING = enum.auto()  # the operator is exiting, the asyncio task is still awaited.
    DAEMON_SIGNALLED = enum.auto()  # the stopper flag was set, the asyncio task is still awaited.
    DAEMON_CANCELLED = enum.auto()  # the asyncio task was cancelled, the thread can be running.
    DAEMON_ABANDONED = enum.auto()  # we gave up on the asyncio task, the thread can be running.


DaemonStopper = FlagSetter[DaemonStoppingReason]
DaemonStopped = FlagWaiter[DaemonStoppingReason]
SyncDaemonStopperChecker = SyncFlagWaiter[DaemonStoppingReason]  # deprecated
AsyncDaemonStopperChecker = AsyncFlagWaiter[DaemonStoppingReason]  # deprecated


async def sleep_or_wait(
        delays: Union[None, float, Collection[Union[None, float]]],
        wakeup: Optional[asyncio.Event] = None,
) -> Optional[float]:
    """
    Measure the sleep time: either until the timeout, or until the event is set.

    Returns the number of seconds left to sleep, or ``None`` if the sleep was
    not interrupted and reached its specified delay (an equivalent of ``0``).
    In theory, the result can be ``0`` if the sleep was interrupted precisely
    the last moment before timing out; this is unlikely to happen though.
    """
    passed_delays = delays if isinstance(delays, collections.abc.Collection) else [delays]
    actual_delays = [delay for delay in passed_delays if delay is not None]
    minimal_delay = min(actual_delays) if actual_delays else 0

    # Do not go for the real low-level system sleep if there is no need to sleep.
    if minimal_delay <= 0:
        return None

    awakening_event = wakeup if wakeup is not None else asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        start_time = loop.time()
        await asyncio.wait_for(awakening_event.wait(), timeout=minimal_delay)
    except asyncio.TimeoutError:
        return None  # interruptable sleep is over: uninterrupted.
    else:
        end_time = loop.time()
        duration = end_time - start_time
        return max(0, minimal_delay - duration)
