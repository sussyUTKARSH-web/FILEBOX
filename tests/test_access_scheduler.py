import threading
import time
import unittest

from src.access_scheduler import ReaderWriterScheduler


class ReaderWriterSchedulerTests(unittest.TestCase):
    def test_two_readers_share_and_writer_waits(self):
        scheduler = ReaderWriterScheduler(aging_interval=5)
        both_reading = threading.Barrier(3)
        release_readers = threading.Event()
        writer_entered = threading.Event()

        def reader():
            with scheduler.acquire("shared-file", "read"):
                both_reading.wait(timeout=2)
                release_readers.wait(timeout=2)

        def writer():
            with scheduler.acquire("shared-file", "write"):
                writer_entered.set()

        readers = [threading.Thread(target=reader) for _ in range(2)]
        for thread in readers:
            thread.start()
        both_reading.wait(timeout=2)
        self.assertEqual(scheduler.snapshot("shared-file")["readers"], 2)
        writer_thread = threading.Thread(target=writer)
        writer_thread.start()
        time.sleep(0.05)
        self.assertFalse(writer_entered.is_set())
        release_readers.set()
        for thread in readers:
            thread.join(timeout=2)
        writer_thread.join(timeout=2)
        self.assertTrue(writer_entered.is_set())

    def test_high_priority_writer_precedes_waiting_low_writer(self):
        acquired = []
        acquired_lock = threading.Lock()
        scheduler = ReaderWriterScheduler(aging_interval=5)
        held = scheduler.acquire("priority-file", "write")

        def writer(label, priority):
            with scheduler.acquire("priority-file", "write", priority=priority):
                with acquired_lock:
                    acquired.append(label)

        low = threading.Thread(target=writer, args=("low", 0))
        low.start()
        deadline = time.monotonic() + 2
        while scheduler.snapshot("priority-file")["queued"] < 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        high = threading.Thread(target=writer, args=("high", 2))
        high.start()
        deadline = time.monotonic() + 2
        while scheduler.snapshot("priority-file")["queued"] < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        held.release()
        low.join(timeout=3)
        high.join(timeout=3)
        self.assertEqual(acquired, ["high", "low"])

    def test_aging_prevents_low_priority_starvation_and_fifo_breaks_tie(self):
        acquired = []
        acquired_lock = threading.Lock()
        scheduler = ReaderWriterScheduler(aging_interval=0.02)
        held = scheduler.acquire("aging-file", "write")

        def writer(label, priority):
            with scheduler.acquire("aging-file", "write", priority=priority):
                with acquired_lock:
                    acquired.append(label)

        low = threading.Thread(target=writer, args=("old-low", 0))
        low.start()
        deadline = time.monotonic() + 2
        while scheduler.snapshot("aging-file")["queued"] < 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        time.sleep(0.07)  # The old request ages to the top priority level.
        high = threading.Thread(target=writer, args=("new-high", 2))
        high.start()
        deadline = time.monotonic() + 2
        while scheduler.snapshot("aging-file")["queued"] < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        held.release()
        low.join(timeout=3)
        high.join(timeout=3)
        self.assertEqual(acquired, ["old-low", "new-high"])

    def test_unrelated_files_do_not_share_lock_state(self):
        scheduler = ReaderWriterScheduler()
        held = scheduler.acquire("file-a", "write")
        with scheduler.acquire("file-b", "write"):
            self.assertTrue(scheduler.snapshot("file-a")["writer"])
            self.assertTrue(scheduler.snapshot("file-b")["writer"])
        held.release()

    def test_exception_releases_read_lock(self):
        scheduler = ReaderWriterScheduler()
        with self.assertRaisesRegex(RuntimeError, "simulated"):
            with scheduler.acquire("exception-file", "read"):
                raise RuntimeError("simulated failure")
        self.assertEqual(scheduler.snapshot("exception-file")["readers"], 0)
        with scheduler.acquire("exception-file", "write"):
            self.assertTrue(scheduler.snapshot("exception-file")["writer"])

    def test_priority_validation(self):
        scheduler = ReaderWriterScheduler()
        with self.assertRaises(ValueError):
            scheduler.acquire("f", "write", priority=9)
        with self.assertRaises(ValueError):
            scheduler.acquire("f", "execute")


if __name__ == "__main__":
    unittest.main()
