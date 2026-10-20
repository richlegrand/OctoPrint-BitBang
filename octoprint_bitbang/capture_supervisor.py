"""The reopen policy, shared by the capture sources.

Stage 2 of #6 gave this to V4l2H264Track alone, which left the picamera2 path
and the software-encode fallback with the behavior it replaced: a capture that
ends stays ended until OctoPrint restarts. Nothing about the policy is V4L2,
so it lives here, and each source supplies only the parts that genuinely
differ -- how to run one capture, and how to tell whether the source might
work again.
"""

import time

# Reopening policy for a capture that ended on its own.
#
# The decision is how long the run lasted, not how many attempts have been
# made. A capture that ran for hours and then stopped is a camera that
# disconnected, and it will very likely come back; retrying that indefinitely
# is right. A capture that ends within seconds, again and again, is a
# configuration that cannot work -- a missing device, an encoder the GPU does
# not have, another process holding the camera -- and retrying it forever only
# hides the problem. So a run that lasts past _RUN_ESTABLISHED clears the
# count, which means a camera that drops once a day never exhausts its budget.
_RUN_ESTABLISHED = 10.0      # seconds; past this, the capture really started
_MAX_FAILED_STARTS = 5       # consecutive sub-_RUN_ESTABLISHED runs before stopping
_REOPEN_BACKOFF = (1.0, 2.0, 5.0, 10.0, 15.0)


class ReopenPolicy:
    """Whether to reopen a capture that just ended, and how long to wait.

    Separate from the loops that drive it, because the two source shapes
    cannot share a loop. A push source -- ffmpeg into a thread, picamera2
    into a callback -- supervises from its worker thread and sleeps on a
    threading.Event. A pull source, aiortc's MediaPlayer, supervises inside
    recv() on the event loop and sleeps with asyncio.sleep. The rules are
    identical either way, so they live here and are tested once.

    Defaults are read at construction, so a test patching the module
    constants before a loop starts gets the values it set.
    """

    def __init__(self, established=None, max_failed_starts=None, backoff=None):
        self.established = _RUN_ESTABLISHED if established is None else established
        self.max_failed_starts = (_MAX_FAILED_STARTS if max_failed_starts is None
                                  else max_failed_starts)
        self.backoff_steps = _REOPEN_BACKOFF if backoff is None else backoff
        self.failed_starts = 0

    def record(self, ran_seconds):
        """Note a capture that ended after `ran_seconds`.

        Returns True if the source should be reopened, False once the budget
        of consecutive failed starts is spent. A run that lasted clears the
        count, so a camera that drops once a day never runs out.
        """
        if ran_seconds >= self.established:
            self.failed_starts = 0
        else:
            self.failed_starts += 1
        return self.failed_starts < self.max_failed_starts

    @property
    def first_failure_of_episode(self):
        """True when this failure deserves the full report rather than a line.

        A run that lasted resets the count to zero, so it reads as first --
        which is right: a disconnect after hours is news every time.
        """
        return self.failed_starts <= 1

    def backoff(self):
        return self.backoff_steps[min(self.failed_starts,
                                      len(self.backoff_steps) - 1)]


class CaptureSupervisor:
    """Run a capture, reopen it when it ends, and decide when to stop trying.

    A source mixing this in must already have, as V4l2H264Track and
    PiH264Track do by being MediaStreamTracks with a worker thread:

      self._stop      threading.Event, set by stop()
      self._logger    where the narration goes
      self._loop      the asyncio loop, so _fail can cross threads
      self._fail(exc) wake recv() with an error

    and must provide:

      _source_name    what to call the thing in a log line
      _run_once()     run one capture until it ends. Return None if we stopped
                      it deliberately, otherwise a short string saying why it
                      ended. Raising is allowed; it is caught and reported.

    and may provide:

      _wait_for_source()   block until the source might work again; default
                           assumes it always might
      _cleanup_capture()   tear down one capture before the next is started
      _report_failure(reason, ran_seconds)
                           the full first-failure report. The default is one
                           line; a source with more to say overrides it
      _report_contention() who else is using the source, if that is knowable
      _capture_tail()      lines from the last capture worth printing when
                           they are ones we have not printed yet
    """

    # -- hooks with defaults --

    @property
    def _source_name(self):
        return getattr(self, "device", self.__class__.__name__)

    def _wait_for_source(self):
        return True

    def _cleanup_capture(self):
        pass

    def _report_failure(self, reason, ran_seconds):
        self._logger.warning(
            f"BitBang: capture on {self._source_name} stopped after "
            f"{ran_seconds:.1f}s: {reason}")

    def _report_contention(self):
        pass

    def _capture_tail(self):
        return []

    # -- the policy --

    def _capture_loop(self):
        """Run the capture, reopening the source when it ends on its own.

        Gives up only when reopening keeps failing straight away. Until then
        the track stays alive and `recv()` simply waits, so a camera that
        drops off shows up as a stall rather than video that never comes
        back."""
        policy = ReopenPolicy()
        reported_tail = []
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                reason = self._run_once()
            except Exception as e:  # noqa: BLE001 - reported, then maybe surfaced
                reason = str(e) or e.__class__.__name__
            finally:
                self._cleanup_capture()
            if self._stop.is_set() or reason is None:
                return

            ran = time.monotonic() - started
            keep_going = policy.record(ran)

            # The first failure of an episode gets the whole story. Repeats
            # get one line, so a camera that drops daily does not fill the
            # journal.
            tail = list(self._capture_tail())
            if policy.first_failure_of_episode:
                self._report_failure(reason, ran)
                self._report_contention()
                reported_tail = tail
            else:
                self._logger.warning(
                    f"BitBang: capture on {self._source_name} failed again "
                    f"after {ran:.1f}s "
                    f"({policy.failed_starts}/{policy.max_failed_starts}): "
                    f"{reason}")
                # ...unless the repeat has something new to say. The first
                # failure of an episode is often the least informative one: a
                # process that was killed prints nothing, and it is the reopen
                # that reports "Device or resource busy". Printing a tail we
                # have not printed yet costs one block per distinct error
                # rather than one per attempt.
                if tail and tail != reported_tail:
                    for line in tail:
                        self._logger.warning(f"BitBang:   {line}")
                    reported_tail = tail

            if not keep_going:
                self._logger.error(
                    f"BitBang: giving up on {self._source_name} after "
                    f"{policy.max_failed_starts} failed starts -- the camera "
                    f"is gone, in use, or configured in a way this machine "
                    f"cannot run")
                # Ask again here rather than relying on the first failure's
                # report: whoever took the source may have taken it after that.
                self._report_contention()
                self._loop.call_soon_threadsafe(
                    self._fail,
                    RuntimeError(
                        f"Capture on {self._source_name} failed: {reason}"))
                return

            if not self._wait_for_source():
                self._logger.warning(
                    f"BitBang: {self._source_name} has not come back; "
                    f"still trying")
            if self._stop.wait(policy.backoff()):
                return
            self._logger.info(f"BitBang: reopening {self._source_name}")
