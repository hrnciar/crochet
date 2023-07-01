"""
Tests for the crochet APIs.
"""

from __future__ import absolute_import

import threading
import subprocess
import time
import gc
import sys
import weakref
import tempfile
import os
import inspect
from unittest import SkipTest

from twisted.trial.unittest import TestCase
from twisted.internet.defer import succeed, Deferred, fail, CancelledError
from twisted.python.failure import Failure
from twisted.python import threadable
from twisted.python.runtime import platform

from .._eventloop import (
    EventLoop, EventualResult, TimeoutError, ResultRegistry, ReactorStopped)
from .test_setup import FakeReactor
from .. import (
    _main, setup as setup_crochet, retrieve_result, _store, no_setup,
    run_in_reactor, wait_for)
from ..tests import crochet_directory

if platform.type == "posix":
    try:
        from twisted.internet.process import reapAllProcesses
    except (SyntaxError, ImportError):
        if sys.version_info < (3, 3, 0):
            raise
        else:
            # Process support is still not ported to Python 3 on some versions
            # of Twisted.
            reapAllProcesses = None
else:
    # waitpid() is only necessary on POSIX:
    reapAllProcesses = None


class ResultRegistryTests(TestCase):
    """
    Tests for ResultRegistry.
    """

    def test_stopped_registered(self):
        """
        ResultRegistery.stop() fires registered EventualResult with
        ReactorStopped.
        """
        registry = ResultRegistry()
        er = EventualResult(None, None)
        registry.register(er)
        registry.stop()
        self.assertRaises(ReactorStopped, er.wait, timeout=0)

    def test_stopped_new_registration(self):
        """
        After ResultRegistery.stop() is called subsequent register() calls
        raise ReactorStopped.
        """
        registry = ResultRegistry()
        er = EventualResult(None, None)
        registry.stop()
        self.assertRaises(ReactorStopped, registry.register, er)

    def test_stopped_already_have_result(self):
        """
        ResultRegistery.stop() has no impact on registered EventualResult
        which already have a result.
        """
        registry = ResultRegistry()
        er = EventualResult(succeed(123), None)
        registry.register(er)
        registry.stop()
        self.assertEqual(er.wait(0.1), 123)
        self.assertEqual(er.wait(0.1), 123)
        self.assertEqual(er.wait(0.1), 123)

    def test_weakref(self):
        """
        Registering an EventualResult with a ResultRegistry does not prevent
        it from being garbage collected.
        """
        registry = ResultRegistry()
        er = EventualResult(None, None)
        registry.register(er)
        ref = weakref.ref(er)
        del er
        gc.collect()
        self.assertIdentical(ref(), None)

    def test_runs_with_lock(self):
        """
        All code in ResultRegistry.stop() and register() is protected by a
        lock.
        """
        self.assertTrue(ResultRegistry.stop.synchronized)
        self.assertTrue(ResultRegistry.register.synchronized)


def append_in_thread(a_list, f, *args, **kwargs):
    """
    Call a function in a thread, append its result to the given list.

    Only return once the thread has actually started.

    Will return a threading.Event that will be set when the action is done.
    """
    started = threading.Event()
    done = threading.Event()

    def go():
        started.set()
        try:
            result = f(*args, **kwargs)
        except Exception as e:
            a_list.extend([False, e])
        else:
            a_list.extend([True, result])
        done.set()

    threading.Thread(target=go).start()
    started.wait()
    return done


class EventualResultTests(TestCase):
    """
    Tests for EventualResult.
    """

    def setUp(self):
        self.patch(threadable, "isInIOThread", lambda: False)

    def test_success_result(self):
        """
        wait() returns the value the Deferred fired with.
        """
        dr = EventualResult(succeed(123), None)
        self.assertEqual(dr.wait(0.1), 123)

    def test_later_success_result(self):
        """
        wait() returns the value the Deferred fired with, in the case where
        the Deferred is fired after wait() is called.
        """
        d = Deferred()
        dr = EventualResult(d, None)
        result_list = []
        done = append_in_thread(result_list, dr.wait, 100)
        time.sleep(0.1)
        # At this point dr.wait() should have started:
        d.callback(345)
        done.wait(100)
        self.assertEqual(result_list, [True, 345])

    def test_success_result_twice(self):
        """
        A second call to wait() returns same value as the first call.
        """
        dr = EventualResult(succeed(123), None)
        self.assertEqual(dr.wait(0.1), 123)
        self.assertEqual(dr.wait(0.1), 123)

    def test_failure_result(self):
        """
        wait() raises the exception the Deferred fired with.
        """
        dr = EventualResult(fail(RuntimeError()), None)
        self.assertRaises(RuntimeError, dr.wait, 0.1)

    def test_later_failure_result(self):
        """
        wait() raises the exception the Deferred fired with, in the case
        where the Deferred is fired after wait() is called.
        """
        d = Deferred()
        dr = EventualResult(d, None)
        result_list = []
        done = append_in_thread(result_list, dr.wait, 100)
        time.sleep(0.1)
        d.errback(RuntimeError())
        done.wait(100)
        self.assertEqual(
            (result_list[0], result_list[1].__class__), (False, RuntimeError))

    def test_failure_result_twice(self):
        """
        A second call to wait() raises same value as the first call.
        """
        dr = EventualResult(fail(ZeroDivisionError()), None)
        self.assertRaises(ZeroDivisionError, dr.wait, 0.1)
        self.assertRaises(ZeroDivisionError, dr.wait, 0.1)

    def test_timeout(self):
        """
        If no result is available, wait(timeout) will throw a TimeoutError.
        """
        start = time.time()
        dr = EventualResult(Deferred(), None)
        self.assertRaises(TimeoutError, dr.wait, timeout=0.03)
        # be a little lenient for slow computers:
        self.assertTrue(abs(time.time() - start) < 0.05)

    def test_timeout_twice(self):
        """
        If no result is available, a second call to wait(timeout) will also
        result in a TimeoutError exception.
        """
        dr = EventualResult(Deferred(), None)
        self.assertRaises(TimeoutError, dr.wait, timeout=0.01)
        self.assertRaises(TimeoutError, dr.wait, timeout=0.01)

    def test_timeout_then_result(self):
        """
        If a result becomes available after a timeout, a second call to
        wait() will return it.
        """
        d = Deferred()
        dr = EventualResult(d, None)
        self.assertRaises(TimeoutError, dr.wait, timeout=0.01)
        d.callback(u"value")
        self.assertEqual(dr.wait(0.1), u"value")
        self.assertEqual(dr.wait(0.1), u"value")

    def test_reactor_thread_disallowed(self):
        """
        wait() cannot be called from the reactor thread.
        """
        self.patch(threadable, "isInIOThread", lambda: True)
        d = Deferred()
        dr = EventualResult(d, None)
        self.assertRaises(RuntimeError, dr.wait, 0)

    def test_cancel(self):
        """
        cancel() cancels the wrapped Deferred, running cancellation in the
        event loop thread.
        """
        reactor = FakeReactor()
        cancelled = []

        def error(f):
            cancelled.append(reactor.in_call_from_thread)
            cancelled.append(f)

        d = Deferred().addErrback(error)
        dr = EventualResult(d, _reactor=reactor)
        dr.cancel()
        self.assertTrue(cancelled[0])
        self.assertIsInstance(cancelled[1].value, CancelledError)

    def test_stash(self):
        """
        EventualResult.stash() stores the object in the global ResultStore.
        """
        dr = EventualResult(Deferred(), None)
        uid = dr.stash()
        self.assertIdentical(dr, _store.retrieve(uid))

    def test_original_failure(self):
        """
        original_failure() returns the underlying Failure of the Deferred
        wrapped by the EventualResult.
        """
        try:
            1 / 0
        except ZeroDivisionError:
            f = Failure()
        dr = EventualResult(fail(f), None)
        self.assertIdentical(dr.original_failure(), f)

    def test_original_failure_no_result(self):
        """
        If there is no result yet, original_failure() returns None.
        """
        dr = EventualResult(Deferred(), None)
        self.assertIdentical(dr.original_failure(), None)

    def test_original_failure_not_error(self):
        """
        If the result is not an error, original_failure() returns None.
        """
        dr = EventualResult(succeed(3), None)
        self.assertIdentical(dr.original_failure(), None)

    def test_error_logged_no_wait(self):
        """
        If the result is an error and wait() was never called, the error will
        be logged once the EventualResult is garbage-collected.
        """
        dr = EventualResult(fail(ZeroDivisionError()), None)
        del dr
        gc.collect()
        excs = self.flushLoggedErrors(ZeroDivisionError)
        self.assertEqual(len(excs), 1)

    def test_error_logged_wait_timeout(self):
        """
        If the result is an error and wait() was called but timed out, the
        error will be logged once the EventualResult is garbage-collected.
        """
        d = Deferred()
        dr = EventualResult(d, None)
        try:
            dr.wait(0)
        except TimeoutError:
            pass
        d.errback(ZeroDivisionError())
        del dr
        if sys.version_info[0] == 2:
            sys.exc_clear()
        gc.collect()
        excs = self.flushLoggedErrors(ZeroDivisionError)
        self.assertEqual(len(excs), 1)

    def test_error_after_gc_logged(self):
        """
        If the result is an error that occurs after all user references to the
        EventualResult are lost, the error is still logged.
        """
        d = Deferred()
        dr = EventualResult(d, None)
        del dr
        d.errback(ZeroDivisionError())
        gc.collect()
        excs = self.flushLoggedErrors(ZeroDivisionError)
        self.assertEqual(len(excs), 1)

    def test_control_c_is_possible(self):
        """
        If you're wait()ing on an EventualResult in main thread, make sure the
        KeyboardInterrupt happens in timely manner.
        """
        if platform.type != "posix":
            raise SkipTest("I don't have the energy to fight Windows semantics.")
        program = """\
import os, threading, signal, time, sys
import crochet
crochet.setup()
from twisted.internet.defer import Deferred

if sys.platform.startswith('win'):
    signal.signal(signal.SIGBREAK, signal.default_int_handler)
    sig_int=signal.CTRL_BREAK_EVENT
    sig_kill=signal.SIGTERM
else:
    sig_int=signal.SIGINT
    sig_kill=signal.SIGKILL


def interrupt():
    time.sleep(0.1) # Make sure we've hit wait()
    os.kill(os.getpid(), sig_int)
    time.sleep(1)
    # Still running, test shall fail...
    os.kill(os.getpid(), sig_kill)

t = threading.Thread(target=interrupt, daemon=True)
t.start()

d = Deferred()
e = crochet.EventualResult(d, None)

try:
    e.wait(10000)
except KeyboardInterrupt:
    sys.exit(23)
"""
        kw = {'cwd': crochet_directory}
        # on Windows the only way to interrupt a subprocess reliably is to
        # create a new process group:
        # http://docs.python.org/2/library/subprocess.html#subprocess.CREATE_NEW_PROCESS_GROUP
        if platform.type.startswith('win'):
            kw['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
        process = subprocess.Popen([sys.executable, "-c", program], **kw)
        self.assertEqual(process.wait(), 23)

    def test_connect_deferred(self):
        """
        If an EventualResult is created with None,
        EventualResult._connect_deferred can be called later to register a
        Deferred as the one it is wrapping.
        """
        er = EventualResult(None, None)
        self.assertRaises(TimeoutError, er.wait, 0)
        d = Deferred()
        er._connect_deferred(d)
        self.assertRaises(TimeoutError, er.wait, 0)
        d.callback(123)
        self.assertEqual(er.wait(0.1), 123)

    def test_reactor_stop_unblocks_EventualResult(self):
        """
        Any EventualResult.wait() calls still waiting when the reactor has
        stopped will get a ReactorStopped exception.
        """
        program = """\
import os, threading, signal, time, sys

from twisted.internet.defer import Deferred
from twisted.internet import reactor

import crochet
crochet.setup()

@crochet.run_in_reactor
def run():
    reactor.callLater(0.1, reactor.stop)
    return Deferred()

er = run()
try:
    er.wait(timeout=10)
except crochet.ReactorStopped:
    sys.exit(23)
"""
        process = subprocess.Popen([sys.executable, "-c", program],
                                   cwd=crochet_directory)
        self.assertEqual(process.wait(), 23)

    def test_reactor_stop_unblocks_EventualResult_in_threadpool(self):
        """
        Any EventualResult.wait() calls still waiting when the reactor has
        stopped will get a ReactorStopped exception, even if it is running in
        Twisted's thread pool.
        """
        program = """\
import os, threading, signal, time, sys

from twisted.internet.defer import Deferred
from twisted.internet import reactor

import crochet
crochet.setup()

@crochet.run_in_reactor
def run():
    reactor.callLater(0.1, reactor.stop)
    return Deferred()

result = [13]
def inthread():
    er = run()
    try:
        er.wait(timeout=10)
    except crochet.ReactorStopped:
        result[0] = 23
reactor.callInThread(inthread)
time.sleep(1)
sys.exit(result[0])
"""
        process = subprocess.Popen([sys.executable, "-c", program],
                                   cwd=crochet_directory)
        self.assertEqual(process.wait(), 23)

    def test_immediate_cancel(self):
        """
        Immediately cancelling the result of @run_in_reactor function will
        still cancel the Deferred.
        """
        # This depends on the way reactor runs callFromThread calls, so need
        # real functional test.
        program = """\
import os, threading, signal, time, sys

from twisted.internet.defer import Deferred, CancelledError

import crochet
crochet.setup()

@crochet.run_in_reactor
def run():
    return Deferred()

er = run()
er.cancel()
try:
    er.wait(1)
except CancelledError:
    sys.exit(23)
else:
    sys.exit(3)
"""
        process = subprocess.Popen(
            [sys.executable, "-c", program],
            cwd=crochet_directory, )
        self.assertEqual(process.wait(), 23)

    def test_noWaitingDuringImport(self):
        """
        EventualResult.wait() raises an exception if called while a module is
        being imported.

        This prevents the imports from taking a long time, preventing other
        imports from running in other threads. It also prevents deadlocks,
        which can happen if the code being waited on also tries to import
        something.
        """
        if sys.version_info[0] > 2:
            from unittest import SkipTest
            raise SkipTest(
                "This test is too fragile (and insufficient) on "
                "Python 3 - see "
                "https://github.com/itamarst/crochet/issues/43")
        directory = tempfile.mktemp()
        os.mkdir(directory)
        sys.path.append(directory)
        self.addCleanup(sys.path.remove, directory)
        with open(os.path.join(directory, "shouldbeunimportable.py"),
                  "w") as f:
            f.write(
                """\
from crochet import EventualResult
from twisted.internet.defer import Deferred

EventualResult(Deferred(), None).wait(1.0)
""")
        self.assertRaises(RuntimeError, __import__, "shouldbeunimportable")


class RunInReactorTests(TestCase):
    """
    Tests for the run_in_reactor decorator.
    """

    def test_signature(self):
        """
        The function decorated with the run_in_reactor decorator has the same
        signature as the original function.
        """
        c = EventLoop(lambda: FakeReactor(), lambda f, g: None)

        def some_name(arg1, arg2, karg1=2, *args, **kw):
            pass
        decorated = c.run_in_reactor(some_name)
        self.assertEqual(inspect.signature(some_name),
                         inspect.signature(decorated))

    def test_name(self):
        """
        The function decorated with run_in_reactor has the same name as the
        original function.
        """
        c = EventLoop(lambda: FakeReactor(), lambda f, g: None)

        @c.run_in_reactor
        def some_name():
            pass

        self.assertEqual(some_name.__name__, "some_name")

    def test_run_in_reactor_thread(self):
        """
        The function decorated with run_in_reactor is run in the reactor
        thread.
        """
        myreactor = FakeReactor()
        c = EventLoop(lambda: myreactor, lambda f, g: None)
        c.no_setup()
        calls = []

        @c.run_in_reactor
        def func(a, b, c):
            self.assertTrue(myreactor.in_call_from_thread)
            calls.append((a, b, c))

        func(1, 2, c=3)
        self.assertEqual(calls, [(1, 2, 3)])

    def test_method(self):
        """
        The function decorated with the wait decorator can be a method.
        """
        myreactor = FakeReactor()
        c = EventLoop(lambda: myreactor, lambda f, g: None)
        c.no_setup()
        calls = []

        class C(object):
            @c.run_in_reactor
            def func(self, a, b, c):
                calls.append((self, a, b, c))

        o = C()
        o.func(1, 2, c=3)
        self.assertEqual(calls, [(o, 1, 2, 3)])

    def test_classmethod(self):
        """
        The function decorated with the wait decorator can be a classmethod.
        """
        myreactor = FakeReactor()
        c = EventLoop(lambda: myreactor, lambda f, g: None)
        c.no_setup()
        calls = []

        class C(object):
            @c.run_in_reactor
            @classmethod
            def func(cls, a, b, c):
                calls.append((cls, a, b, c))

            @classmethod
            @c.run_in_reactor
            def func2(cls, a, b, c):
                calls.append((cls, a, b, c))

        C.func(1, 2, c=3)
        C.func2(1, 2, c=3)
        self.assertEqual(calls, [(C, 1, 2, 3), (C, 1, 2, 3)])

    def test_wrap_method(self):
        """
        The object decorated with the wait decorator can be a method object
        """
        myreactor = FakeReactor()
        c = EventLoop(lambda: myreactor, lambda f, g: None)
        c.no_setup()
        calls = []

        class C(object):
            def func(self, a, b, c):
                calls.append((a, b, c))

        f = c.run_in_reactor(C().func)
        f(4, 5, c=6)
        self.assertEqual(calls, [(4, 5, 6)])

    def make_wrapped_function(self):
        """
        Return a function wrapped with run_in_reactor that returns its first
        argument.
        """
        myreactor = FakeReactor()
        c = EventLoop(lambda: myreactor, lambda f, g: None)
        c.no_setup()

        @c.run_in_reactor
        def passthrough(argument):
            return argument

        return passthrough

    def test_deferred_success_result(self):
        """
        If the underlying function returns a Deferred, the wrapper returns a
        EventualResult hooked up to the Deferred.
        """
        passthrough = self.make_wrapped_function()
        result = passthrough(succeed(123))
        self.assertIsInstance(result, EventualResult)
        self.assertEqual(result.wait(0.1), 123)

    def test_deferred_failure_result(self):
        """
        If the underlying function returns a Deferred, the wrapper returns a
        EventualResult hooked up to the Deferred that can deal with failures
        as well.
        """
        passthrough = self.make_wrapped_function()
        result = passthrough(fail(ZeroDivisionError()))
        self.assertIsInstance(result, EventualResult)
        self.assertRaises(ZeroDivisionError, result.wait, 0.1)

    def test_regular_result(self):
        """
        If the underlying function returns a non-Deferred, the wrapper returns
        a EventualResult hooked up to a Deferred wrapping the result.
        """
        passthrough = self.make_wrapped_function()
        result = passthrough(123)
        self.assertIsInstance(result, EventualResult)
        self.assertEqual(result.wait(0.1), 123)

    def test_exception_result(self):
        """
        If the underlying function throws an exception, the wrapper returns a
        EventualResult hooked up to a Deferred wrapping the exception.
        """
        myreactor = FakeReactor()
        c = EventLoop(lambda: myreactor, lambda f, g: None)
        c.no_setup()

        @c.run_in_reactor
        def raiser():
            1 / 0

        result = raiser()
        self.assertIsInstance(result, EventualResult)
        self.assertRaises(ZeroDivisionError, result.wait, 0.1)

    def test_registry(self):
        """
        @run_in_reactor registers the EventualResult in the ResultRegistry.
        """
        myreactor = FakeReactor()
        c = EventLoop(lambda: myreactor, lambda f, g: None)
        c.no_setup()

        @c.run_in_reactor
        def run():
            return

        result = run()
        self.assertIn(result, c._registry._results)

    def test_wrapped_function(self):
        """
        The function wrapped by @run_in_reactor can be accessed via the
        `__wrapped__` attribute.
        """
        c = EventLoop(lambda: None, lambda f, g: None)

        def func():
            pass

        wrapper = c.run_in_reactor(func)
        self.assertIdentical(wrapper.__wrapped__, func)

    def test_async_function(self):
        """
        Async functions can be wrapped with @run_in_reactor.
        """
        myreactor = FakeReactor()
        c = EventLoop(lambda: myreactor, lambda f, g: None)
        c.no_setup()
        calls = []

        @c.run_in_reactor
        async def go():
            self.assertTrue(myreactor.in_call_from_thread)
            calls.append(1)
            return 23

        self.assertEqual((go().wait(0.1), go().wait(0.1)), (23, 23))
        self.assertEqual(len(calls), 2)
        self.assertFalse(inspect.iscoroutinefunction(go))


class WaitTests(TestCase):
    """
    Tests for wait_for decorators.
    """

    def setUp(self):
        self.reactor = FakeReactor()
        self.eventloop = EventLoop(lambda: self.reactor, lambda f, g: None)
        self.eventloop.no_setup()

    DECORATOR_CALL = "wait_for(timeout=5)"

    def decorator(self):
        return lambda func: self.eventloop.wait_for(timeout=5)(func)

    def make_wrapped_function(self):
        """
        Return a function wrapped with the decorator being tested that returns
        its first argument, or raises it if it's an exception.
        """
        decorator = self.decorator()

        @decorator
        def passthrough(argument):
            if isinstance(argument, Exception):
                raise argument
            return argument

        return passthrough

    def test_name(self):
        """
        The function decorated with the wait decorator has the same name as the
        original function.
        """
        decorator = self.decorator()

        @decorator
        def some_name(argument):
            pass

        self.assertEqual(some_name.__name__, "some_name")

    def test_signature(self):
        """
        The function decorated with the wait decorator has the same signature
        as the original function.
        """
        decorator = self.decorator()

        def some_name(arg1, arg2, karg1=2, *args, **kw):
            pass
        decorated = decorator(some_name)
        self.assertEqual(inspect.signature(some_name),
                         inspect.signature(decorated))

    def test_wrapped_function(self):
        """
        The function wrapped by the wait decorator can be accessed via the
        `__wrapped__` attribute.
        """
        decorator = self.decorator()

        def func():
            pass

        wrapper = decorator(func)
        self.assertIdentical(wrapper.__wrapped__, func)

    def test_reactor_thread_disallowed(self):
        """
        Functions decorated with the wait decorator cannot be called from the
        reactor thread.
        """
        self.patch(threadable, "isInIOThread", lambda: True)
        f = self.make_wrapped_function()
        self.assertRaises(RuntimeError, f, None)

    def test_wait_for_reactor_thread(self):
        """
        The function decorated with the wait decorator is run in the reactor
        thread.
        """
        in_call_from_thread = []
        decorator = self.decorator()

        @decorator
        def func():
            in_call_from_thread.append(self.reactor.in_call_from_thread)

        in_call_from_thread.append(self.reactor.in_call_from_thread)
        func()
        in_call_from_thread.append(self.reactor.in_call_from_thread)
        self.assertEqual(in_call_from_thread, [False, True, False])

    def test_arguments(self):
        """
        The function decorated with wait decorator gets all arguments passed
        to the wrapper.
        """
        calls = []
        decorator = self.decorator()

        @decorator
        def func(a, b, c):
            calls.append((a, b, c))

        func(1, 2, c=3)
        self.assertEqual(calls, [(1, 2, 3)])

    def test_classmethod(self):
        """
        The function decorated with the wait decorator can be a classmethod.
        """
        calls = []
        decorator = self.decorator()

        class C(object):
            @decorator
            @classmethod
            def func(cls, a, b, c):
                calls.append((a, b, c))

            @classmethod
            @decorator
            def func2(cls, a, b, c):
                calls.append((a, b, c))

        C.func(1, 2, c=3)
        C.func2(1, 2, c=3)
        self.assertEqual(calls, [(1, 2, 3), (1, 2, 3)])

    def test_deferred_success_result(self):
        """
        If the underlying function returns a Deferred, the wrapper returns a
        the Deferred's result.
        """
        passthrough = self.make_wrapped_function()
        result = passthrough(succeed(123))
        self.assertEqual(result, 123)

    def test_deferred_failure_result(self):
        """
        If the underlying function returns a Deferred with an errback, the
        wrapper throws an exception.
        """
        passthrough = self.make_wrapped_function()
        self.assertRaises(
            ZeroDivisionError, passthrough, fail(ZeroDivisionError()))

    def test_regular_result(self):
        """
        If the underlying function returns a non-Deferred, the wrapper returns
        that result.
        """
        passthrough = self.make_wrapped_function()
        result = passthrough(123)
        self.assertEqual(result, 123)

    def test_exception_result(self):
        """
        If the underlying function throws an exception, the wrapper raises
        that exception.
        """
        raiser = self.make_wrapped_function()
        self.assertRaises(ZeroDivisionError, raiser, ZeroDivisionError())

    def test_control_c_is_possible(self):
        """
        A call to a decorated function responds to a Ctrl-C (i.e. with a
        KeyboardInterrupt) in a timely manner.
        """
        if platform.type != "posix":
            raise SkipTest("I don't have the energy to fight Windows semantics.")
        program = """\
import os, threading, signal, time, sys
import crochet
crochet.setup()
from twisted.internet.defer import Deferred

if sys.platform.startswith('win'):
    signal.signal(signal.SIGBREAK, signal.default_int_handler)
    sig_int=signal.CTRL_BREAK_EVENT
    sig_kill=signal.SIGTERM
else:
    sig_int=signal.SIGINT
    sig_kill=signal.SIGKILL


def interrupt():
    time.sleep(0.1) # Make sure we've hit wait()
    os.kill(os.getpid(), sig_int)
    time.sleep(1)
    # Still running, test shall fail...
    os.kill(os.getpid(), sig_kill)

t = threading.Thread(target=interrupt, daemon=True)
t.start()

@crochet.%s
def wait():
    return Deferred()

try:
    wait()
except KeyboardInterrupt:
    sys.exit(23)
""" % (self.DECORATOR_CALL, )
        kw = {'cwd': crochet_directory}
        if platform.type.startswith('win'):
            kw['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
        process = subprocess.Popen([sys.executable, "-c", program], **kw)
        self.assertEqual(process.wait(), 23)

    def test_reactor_stop_unblocks(self):
        """
        Any @wait_for_reactor-decorated calls still waiting when the reactor
        has stopped will get a ReactorStopped exception.
        """
        program = """\
import os, threading, signal, time, sys

from twisted.internet.defer import Deferred
from twisted.internet import reactor

import crochet
crochet.setup()

@crochet.%s
def run():
    reactor.callLater(0.1, reactor.stop)
    return Deferred()

try:
    er = run()
except crochet.ReactorStopped:
    sys.exit(23)
""" % (self.DECORATOR_CALL, )
        process = subprocess.Popen([sys.executable, "-c", program],
                                   cwd=crochet_directory)
        self.assertEqual(process.wait(), 23)

    def test_timeoutRaises(self):
        """
        If a function wrapped with wait_for hits the timeout, it raises
        TimeoutError.
        """

        @self.eventloop.wait_for(timeout=0.5)
        def times_out():
            return Deferred().addErrback(lambda f: f.trap(CancelledError))

        start = time.time()
        self.assertRaises(TimeoutError, times_out)
        self.assertTrue(abs(time.time() - start - 0.5) < 0.1)

    def test_timeoutCancels(self):
        """
        If a function wrapped with wait_for hits the timeout, it cancels
        the underlying Deferred.
        """
        result = Deferred()
        error = []
        result.addErrback(error.append)

        @self.eventloop.wait_for(timeout=0.0)
        def times_out():
            return result

        self.assertRaises(TimeoutError, times_out)
        self.assertIsInstance(error[0].value, CancelledError)

    def test_async_function(self):
        """
        Async functions can be wrapped with @wait_for.
        """
        @self.eventloop.wait_for(timeout=0.1)
        async def go():
            self.assertTrue(self.reactor.in_call_from_thread)
            return 17

        self.assertEqual((go(), go()), (17, 17))
        self.assertFalse(inspect.iscoroutinefunction(go))


class PublicAPITests(TestCase):
    """
    Tests for the public API.
    """

    def test_no_sideeffects(self):
        """
        Creating an EventLoop object, as is done in crochet.__init__, does not
        call any methods on the objects it is created with.
        """
        c = EventLoop(
            lambda: None,
            lambda f, g: 1 / 0,
            lambda *args: 1 / 0,
            watchdog_thread=object(),
            reapAllProcesses=lambda: 1 / 0)
        del c

    def test_eventloop_api(self):
        """
        An EventLoop object configured with the real reactor and
        _shutdown.register is exposed via its public methods.
        """
        from twisted.python.log import startLoggingWithObserver
        from crochet import _shutdown
        self.assertIsInstance(_main, EventLoop)
        self.assertEqual(_main.setup, setup_crochet)
        self.assertEqual(_main.no_setup, no_setup)
        self.assertEqual(_main.run_in_reactor, run_in_reactor)
        self.assertEqual(_main.wait_for, wait_for)
        self.assertIdentical(_main._atexit_register, _shutdown.register)
        self.assertIdentical(
            _main._startLoggingWithObserver, startLoggingWithObserver)
        self.assertIdentical(_main._watchdog_thread, _shutdown._watchdog)

    def test_eventloop_api_reactor(self):
        """
        The publicly exposed EventLoop will, when setup, use the global
        reactor.
        """
        from twisted.internet import reactor
        _main.no_setup()
        self.assertIdentical(_main._reactor, reactor)

    def test_retrieve_result(self):
        """
        retrieve_result() calls retrieve() on the global ResultStore.
        """
        dr = EventualResult(Deferred(), None)
        uid = dr.stash()
        self.assertIdentical(dr, retrieve_result(uid))

    def test_reapAllProcesses(self):
        """
        An EventLoop object configured with the real reapAllProcesses on POSIX
        plaforms.
        """
        self.assertIdentical(_main._reapAllProcesses, reapAllProcesses)

    if platform.type != "posix":
        test_reapAllProcesses.skip = "Only relevant on POSIX platforms"
    if reapAllProcesses is None:
        test_reapAllProcesses.skip = "Twisted does not yet support processes"
