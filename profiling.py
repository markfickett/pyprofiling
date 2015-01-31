import collections
import threading
import time


_REPORT_INTERVAL_S = 10.0
_INDENT = '  '
_Report = collections.namedtuple(
    'Report', ('is_root', 'name', 'children', 'durations'))


class Profiled(object):
  _stack = []
  _last_report_time = time.time()
  _reports = {}
  _lock = threading.Lock()

  def __init__(self, name):
    with self._lock:
      self._report = self._reports.get(name)
    if not self._report:
      self._report = _Report(
          is_root=not self._stack,
          name=name,
          children=[],
          durations=[])
      with self._lock:
        if self._stack:
          self._stack[-1].children.append(self._report)
        self._reports[name] = self._report

  def __enter__(self):
    self._start = time.time()
    with self._lock:
      self._stack.append(self._report)

  def __exit__(self, excClass, excObj, tb):
    self._report.durations.append(time.time() - self._start)
    with self._lock:
      self._stack.pop()
      self._MaybePrintReport()

  @classmethod
  def _MaybePrintReport(cls):
    if (cls._stack or
        time.time() - Profiled._last_report_time < _REPORT_INTERVAL_S):
      return
    cls._last_report_time = time.time()
    for report in cls._reports.values():
      if report.is_root:
        cls._PrintReport(report, 0)
    cls._reports = {}

  @classmethod
  def _PrintReport(cls, report, level):
    total = sum(report.durations)
    print (
        '%s%s %d * %.2fs = %.2fs' %
        (level * _INDENT,
         report.name,
         len(report.durations),
         total / len(report.durations),
         total))
    for child in report.children:
      cls._PrintReport(child, level + 1)
