"""Centralized progress bars.

Two behaviours, both aimed at making ``debug_script.sh`` (which tee's stdout and
stderr into ``debug_run.log``) readable:

* **One bar at a time.** A bar opened while another is already active disables
  itself, so a per-preset outer loop and the per-sample work running beneath it
  never stack into two visible bars.
* **Bars go to the terminal only.** The animated bar is written to the
  controlling terminal (``/dev/tty``) instead of stderr, so a redirected /
  ``tee``-d stdout+stderr keeps the log messages but not the carriage-return bar
  frames. ``tqdm.write`` and log records still go to stdout, so they are
  captured by the redirect as before.
"""
from tqdm import tqdm

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
