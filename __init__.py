import collections
import logging
import threading
import time


_REPORT_INTERVAL_S = 1.0
_INDENT = '  '
_MIN_REMAINDER = 0.01


class Profiled(object):
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
    if time.time() - Profiled._last_report_time < _REPORT_INTERVAL_S:
      return
    cls._last_report_time = time.time()
    root_reports = [r for r in cls._reports_by_name.values() if r.level == 0]
    for report in root_reports:
      logging.info('\n'.join([''] + cls._GetReportLines(report, 0)))

    cls._reports_by_name = {}
    for root in root_reports:
      cls._PruneReports(None, root)

  @classmethod
  def _PruneReports(cls, parent, report):
    if not (report.start is not None or
        (parent and not parent.durations)):
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
  def __init__(self, name, level):
    self.name = name
    self.level = level
    self.children = []
    self.start = None
    self.durations = []
    self.past_child_durations = 0


if __name__ == '__main__':
  import random
  logging.basicConfig(
      format='%(levelname)s %(asctime)s %(filename)s:%(lineno)s: %(message)s',
      level=logging.INFO)
  with Profiled('longstanding root'):
    while True:
      with Profiled('main'):
        for _ in xrange(10):
          with Profiled('short'):
            r = random.random()
          with Profiled('will report outlier max'):
            if r > 0.95:
              time.sleep(1.0)
        with Profiled('long with subsection'):
          for _ in xrange(1000):
            for _ in xrange(100):
              with Profiled('extremely frequent'):
                pass
        time.sleep(r)  # reported in remainder
