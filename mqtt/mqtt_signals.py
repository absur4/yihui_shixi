"""Lock-free shared flags survive intentional endpoint termination on Windows."""
import time


class Signal:
    def __init__(self, context):
        self.flag = context.RawValue('b', 0)

    def set(self):
        self.flag.value = 1

    def clear(self):
        self.flag.value = 0

    def is_set(self):
        return bool(self.flag.value)

    def wait(self, timeout=None):
        end = time.perf_counter()+timeout if timeout is not None else float('inf')
        while not self.is_set():
            remaining = end-time.perf_counter()
            if remaining <= 0:
                return False
            time.sleep(min(.005, remaining))
        return True
