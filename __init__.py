import collections
import logging
import threading
import time


_REPORT_INTERVAL_S = 1.0
_INDENT = '  '
_MIN_REMAINDER = 0.01


class Profiled(object):
  # Shared state; _lock must be held while using these values.
  _stacks_by_threadid = collections.defaultdict(lambda: list())
  _last_report_time = time.time()
  _reports_by_name = {}
  _lock = threading.Lock()

  def __init__(self, name):
    with self._lock:
      self._stack = self._stacks_by_threadid[threading.current_thread().ident]
      self._report = self._reports_by_name.get(name)
    if not self._report:
      self._report = _Report(name, len(self._stack))
      if self._stack:
        self._stack[-1].children.append(self._report)
      with self._lock:
        self._reports_by_name[name] = self._report

  def __enter__(self):
    self._report.start = time.time()
    self._stack.append(self._report)

  def __exit__(self, excClass, excObj, tb):
    self._report.durations.append(time.time() - self._report.start)
    self._report.start = None
    self._stack.pop()
    with self._lock:
      self._MaybePrintReport(self._stack)

  @classmethod
  def _MaybePrintReport(cls, stack):
    # Always print if we're exiting the root profiled code, to avoid losing
    # data. Otherwise, wait to print until the reporting interval has elapsed.
    if stack and (
        time.time() - Profiled._last_report_time < _REPORT_INTERVAL_S):
      return

    # Log all available reports.
    cls._last_report_time = time.time()
    root_reports = [r for r in cls._reports_by_name.values() if r.level == 0]
    for report in root_reports:
      logging.info('\n'.join([''] + cls._GetReportLines(report, 0)))

    # After printing report details, remove anything that's not current. Thus
    # each reporting period covers only what happened since the last one.
    # (Without pruning, reports would be cumulative.)
    cls._reports_by_name = {}
    for root in root_reports:
      cls._PruneReports(None, root)

  @classmethod
  def _PruneReports(cls, parent, report):
    """
    Returns:
      True if this report should be kept around, False if it can be dropped.
    """
    # Keep reports which are currently in context.
    if not (report.start is not None or (parent and not parent.durations)):
      return False

    cls._reports_by_name[report.name] = report
    old_children = list(report.children)
    report.children = []
    for child in old_children:
      old_duration = sum(child.durations)
      if cls._PruneReports(report, child):
        report.children.append(child)
      report.past_child_durations += old_duration
    report.durations = []
    return True

  @classmethod
  def _GetReportLines(cls, report, level):
    """Format one timing summary line for this report, plus lines for children.

    Returns:
      A list of lines like "<report name> 2 * 1.16s = 2.32s". Each line will
      have an appropriate amount of whitespace padding for indentation, based
      on the given level.
    """
    lines = []
    if report.durations:
      total = sum(report.durations)
      ave = total / len(report.durations)
      max_value = max(report.durations)
      lines.append(
          '%s%s %d * %.2fs = %.2fs%s' %
          (level * _INDENT,
           report.name,
           len(report.durations),
           ave,
           total,
           '' if max_value < ave * 10 else ' (max %.2fs)' % max_value))
    elif report.start is not None:
      total = time.time() - report.start
      lines.append(
          '%s%s %.2fs (partial)'
          % (level * _INDENT, report.name, total))
    else:
      total = 0
      lines.append('%s%s (cleared)' % (level * _INDENT, report.name))
    total -= report.past_child_durations
    for child in report.children:
      lines += cls._GetReportLines(child, level + 1)
      total -= sum(child.durations)
    if report.children and total >= _MIN_REMAINDER:
      lines.append('%sremainder %.2fs' % ((level + 1) * _INDENT, total))
    return lines


class _Report(object):
  """Record of past execution times for one profiled block of code."""
  def __init__(self, name, level):
    # An informational / user-provided name for the profiled block of code.
    self.name = name

    # For formatting, how deeply nested the profiled code is.
    self.level = level

    # When this block of code was most recently started. None if the block
    # is not currently running (not in context).
    self.start = None

    # List of durations (in seconds) this code block has taken. Used to
    # summarize how ave/max times.
    self.durations = []

    self.past_child_durations = 0

    # List of child _Reports. All children are executed fully within the parent,
    # but there may be some parent execution time which is not subdivided
    # (reported as "remainder").
    self.children = []


if __name__ == '__main__':
  import random
  logging.basicConfig(
      format='%(levelname)s %(asctime)s %(filename)s:%(lineno)s: %(message)s',
      level=logging.INFO)

  # At its simplest, we can wrap a block of code in a Profiled context guard,
  # and see its timing information printed out when it's done.
  with Profiled('initial block, should take about 1 second'):
    time.sleep(1.0)

  # We can also wrap a long-running (or even an infinite loop), and get periodic
  # reports of how it's currently performing.
  with Profiled('longstanding root'):
    while True:
      # This is a child Profiled.
      with Profiled('main'):
        for _ in xrange(10):
          with Profiled('short'):
            r = random.random()
          # Usually, only the average execution time is reported. But if a block
          # of code has highly variable execution time, the max is reported too.
          with Profiled('will report outlier max'):
            if r > 0.95:
              time.sleep(1.0)
        with Profiled('long with subsection'):
          for _ in xrange(1000):
            for _ in xrange(100):
              with Profiled('extremely frequent'):
                pass
        # This statement is part of the 'main' block, but not in any of its
        # children. It gets called out in the 'remainder', so we notice that
        # the sum of the children is less than the parent.
        time.sleep(r)
