"""Centralized terminal-output hygiene: progress bars and plugin-log muting.

The progress bar has two behaviours, both aimed at making ``debug_script.sh``
(which tee's stdout and stderr into ``debug_run.log``) readable:

* **One bar at a time.** A bar opened while another is already active disables
  itself, so a per-preset outer loop and the per-sample work running beneath it
  never stack into two visible bars.
* **Bars go to the terminal only.** The animated bar is written to the
  controlling terminal (``/dev/tty``) instead of stderr, so a redirected /
  ``tee``-d stdout+stderr keeps the log messages but not the carriage-return bar
  frames. ``tqdm.write`` and log records still go to stdout, so they are
  captured by the redirect as before.

``suppressed_plugin_output`` silences the page of C-level chatter that native
plugins print on every render (see its docstring) so only the bar and our own
status lines remain.
"""
import os
import sys
from contextlib import contextmanager

from tqdm import tqdm

_KEEP_PLUGIN_LOG_ENV = "PATCH_PRESS_PLUGIN_LOG"


@contextmanager
def suppressed_plugin_output():
    """Silence chatter that native render code writes straight to fd 1/2.

    CLAP/VST plugins — u-he Diva especially — print a page of startup/teardown
    banners on every instantiation using C-level ``printf``/``fprintf`` on the
    process's real stdout and stderr, bypassing ``sys.stdout`` (so a Python
    ``redirect_stdout`` can't catch them). Around a native render this points the
    stdout/stderr *file descriptors* at ``/dev/null`` and restores them after, so
    the terminal keeps only our progress bar and per-preset status lines.

    Set ``PATCH_PRESS_PLUGIN_LOG=1`` to keep the chatter (host debugging). The
    progress bar is drawn on ``/dev/tty`` — a different fd — so it is unaffected
    either way. If stdout/stderr aren't real OS fds (closed, or replaced), this
    is a no-op rather than an error: muting logs must never break a render.
    """
    if os.environ.get(_KEEP_PLUGIN_LOG_ENV):
        yield
        return
    try:
        saved = (os.dup(1), os.dup(2))
    except OSError:
        yield
        return
    sys.stdout.flush()
    sys.stderr.flush()
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        yield
    finally:
        # Flush every buffer while the fds still point at /dev/null: Python's own
        # (in case something printed during the block) and libc's C stdio, which
        # the plugin's ``printf`` uses and which is fully buffered when stdout is
        # piped (``debug_script.sh``'s tee) — otherwise its tail would flush onto
        # the terminal only after we restore fd 1.
        sys.stdout.flush()
        sys.stderr.flush()
        _flush_c_stdio()
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        for fd in (*saved, devnull):
            os.close(fd)


def _flush_c_stdio():
    """``fflush(NULL)`` — flush all C stdio output streams (best effort)."""
    try:
        import ctypes

        ctypes.CDLL(None).fflush(None)
    except Exception:
        pass


_bar_file = None
_bar_file_resolved = False


def _terminal_stream():
    """The controlling terminal (``/dev/tty``), or ``None`` if there isn't one.

    Bars are drawn here so a redirected / ``tee``-d stdout+stderr never captures
    the animation. When there's no controlling terminal (cron, CI, ``nohup``)
    there's nothing to watch a bar on, so callers disable the bar rather than
    fall back to stderr and leak frames into the redirect.

    Resolved lazily and cached so worker subprocesses (which import this module
    but never draw bars) don't open ``/dev/tty`` needlessly. The handle lives for
    the life of the process, which ends with ``os._exit`` — no need to close it.
    """
    global _bar_file, _bar_file_resolved
    if not _bar_file_resolved:
        _bar_file_resolved = True
        try:
            _bar_file = open("/dev/tty", "w")  # noqa: SIM115
        except OSError:
            _bar_file = None
    return _bar_file


class ProgressBar(tqdm):
    """``tqdm`` that shows a single, terminal-only bar.

    Drop-in replacement: ``ProgressBar(...)`` and the inherited
    ``ProgressBar.write(...)`` behave like ``tqdm``.
    """

    _depth = 0

    def __init__(self, *args, **kwargs):
        stream = _terminal_stream()
        if stream is None or ProgressBar._depth > 0:
            # No terminal to draw on, or a bar is already on screen — suppress
            # this one so frames never leak into a redirect and the terminal
            # shows at most one bar at a time.
            kwargs["disable"] = True
        else:
            kwargs.setdefault("file", stream)
        self._counts = not kwargs.get("disable", False)
        if self._counts:
            ProgressBar._depth += 1
        super().__init__(*args, **kwargs)

    def close(self):
        super().close()
        if getattr(self, "_counts", False):
            ProgressBar._depth -= 1
            self._counts = False
