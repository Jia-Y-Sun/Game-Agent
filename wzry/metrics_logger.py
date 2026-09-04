import json
import os
import threading
from queue import Empty, Queue


class StepMetricsLogger:
    def __init__(self, path):
        self.path = path
        self.queue = Queue()
        self.stop_event = threading.Event()
        parent_dir = os.path.dirname(path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)

        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

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
