import collections
import logging
import threading
import time


_REPORT_INTERVAL_S = 10.0
_INDENT = '  '
_MIN_REMAINDER = 0.01
_Report = collections.namedtuple(
    'Report', ('is_root', 'name', 'children', 'durations'))


class Profiled(object):
  _stacks_by_threadid = collections.defaultdict(lambda: list())
  _last_report_time = time.time()
  _reports = {}
  _lock = threading.Lock()

  def __init__(self, name):
    with self._lock:
      self._stack = self._stacks_by_threadid[threading.current_thread().ident]
      self._report = self._reports.get(name)
    if not self._report:
      self._report = _Report(
          is_root=not self._stack,
          name=name,
          children=[],
          durations=[])
      if self._stack:
        self._stack[-1].children.append(self._report)
      with self._lock:
        self._reports[name] = self._report

  def __enter__(self):
    self._start = time.time()
    self._stack.append(self._report)

  def __exit__(self, excClass, excObj, tb):
    self._report.durations.append(time.time() - self._start)
    self._stack.pop()
    with self._lock:
      self._MaybePrintReport(self._stack)

  @classmethod
  def _MaybePrintReport(cls, stack):
    if (stack or
        time.time() - Profiled._last_report_time < _REPORT_INTERVAL_S):
      return
    cls._last_report_time = time.time()
    for report in cls._reports.values():
      if report.is_root:
        logging.info('\n'.join([''] + cls._GetReportLines(report, 0)))
    cls._reports = {}

  @classmethod
  def _GetReportLines(cls, report, level):
    total = sum(report.durations)
    ave = total / len(report.durations)
    max_value = max(report.durations)
    lines = []
    lines.append(
        '%s%s %d * %.2fs = %.2fs%s' %
        (level * _INDENT,
         report.name,
         len(report.durations),
         ave,
         total,
         '' if max_value < ave * 10 else ' (max %.2fs)' % max_value))
    for child in report.children:
      lines += cls._GetReportLines(child, level + 1)
      total -= sum(child.durations)
    if report.children and total >= _MIN_REMAINDER:
      lines.append('%sremainder %.2fs' % ((level + 1) * _INDENT, total))
    return lines
