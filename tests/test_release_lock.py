"""Native release coordination; no services or applications are started."""
import importlib.util
import os
from pathlib import Path
import tempfile
import threading
import unittest

spec = importlib.util.spec_from_file_location("release_lock_tested", Path(__file__).resolve().parents[1] / "packaging/release_lock.py")
lock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lock)


@unittest.skipUnless(os.name == "nt", "Windows named mutexes")
class ReleaseLockTests(unittest.TestCase):
    def test_nested_same_thread_release_steps_are_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            with lock.release_lock(Path(temporary)):
                with lock.release_lock(Path(temporary)):
                    pass
            with lock.release_lock(Path(temporary)):
                pass

    def test_other_thread_cannot_clean_during_build_and_lock_releases_after_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outcomes = []
            def contender():
                try:
                    with lock.release_lock(root):
                        outcomes.append("acquired")
                except lock.ReleaseBusyError:
                    outcomes.append("busy")
            with self.assertRaisesRegex(RuntimeError, "synthetic build failure"):
                with lock.release_lock(root):
                    worker = threading.Thread(target=contender)
                    worker.start()
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
                    raise RuntimeError("synthetic build failure")
            self.assertEqual(outcomes, ["busy"])
            contender()
            self.assertEqual(outcomes, ["busy", "acquired"])
