import datetime
import json
import os
import threading
from queue import Empty, Queue


class StepMetricsLogger:
    """逐步指标写入器（后台线程 + 队列）。

    新增：文件超过 `max_mb` 时自动轮转（把旧文件改名成
    `step_metrics_<时间戳>.jsonl`），避免长时间挂机把单个 jsonl 写成几百 MB。
    """

    def __init__(self, path, max_mb=0.0):
        self.path = path
        self.max_mb = float(max_mb or 0.0)
        self.queue = Queue()
        self.stop_event = threading.Event()
        parent_dir = os.path.dirname(path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        self._rotate_if_needed()

        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    # -- P8：按体积轮转 -----------------------------------------------------
    def _rotate_if_needed(self):
        if self.max_mb <= 0 or not os.path.exists(self.path):
            return
        try:
            size_mb = os.path.getsize(self.path) / 1024.0 / 1024.0
        except OSError:
            return
        if size_mb < self.max_mb:
            return
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        root, ext = os.path.splitext(self.path)
        target = f"{root}_{stamp}{ext or '.jsonl'}"
        suffix = 1
        while os.path.exists(target):
            suffix += 1
            target = f"{root}_{stamp}_{suffix}{ext or '.jsonl'}"
        try:
            os.replace(self.path, target)
            print(f"[metrics] 指标文件 {size_mb:.1f}MB 超过上限 {self.max_mb}MB，已轮转为 {target}")
        except OSError as exc:
            print(f"[metrics] 指标文件轮转失败: {exc}")

    def _run(self):
        while not self.stop_event.is_set() or not self.queue.empty():
            try:
                record = self.queue.get(timeout=0.2)
            except Empty:
                continue

            with open(self.path, "a", encoding="utf-8") as file:
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.queue.task_done()

    def log(self, record):
        self.queue.put(record)

    def close(self):
        self.stop_event.set()
        self.queue.join()
        self.worker.join(timeout=1.0)
