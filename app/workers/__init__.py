"""离线任务进程包（V0.1 · T18）。

★ 本包内的代码**只允许**由独立进程调用（python -m app.workers.xxx 或
  compose 的 worker 服务），禁止被 api 层 import 后同步执行（R10）。
"""
