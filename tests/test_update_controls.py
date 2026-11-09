"""Settings update flow without Tk windows, network, installers or processes."""
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import update_controls as updates


class Value:
    def __init__(self, master=None, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class UpdateControlsTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(updates.tk, "StringVar", Value))
        self.enterContext(patch.object(updates.ttk, "Frame"))
        self.enterContext(patch.object(updates.ttk, "Label"))
        self.enterContext(patch.object(updates.ttk, "Button"))
        self.clock = self.enterContext(patch.object(updates.time, "monotonic", return_value=1000))
        self.worker = self.enterContext(patch.object(updates.threading, "Thread"))
        self.check = self.enterContext(patch.object(updates.app_updater, "check_for_update", return_value=None))
        self.support = self.enterContext(patch.object(updates.app_updater, "installation_support", return_value=(True, "")))
        self.prepare = self.enterContext(patch.object(updates.app_updater, "prepare_update"))
        self.discard = self.enterContext(patch.object(updates.app_updater, "discard_update"))
        self.ready = Mock()
        self.panel = updates.UpdateControls(Mock(), current_version="1.6.0", on_ready=self.ready)
        self.release = SimpleNamespace(version="1.7.0", file="Lineage-2-Chat-Translator-1.7.0-Setup.exe",
                                       bytes=1234, sha256="a" * 64)
        self.prepared = SimpleNamespace(release=self.release, stage=Path("C:/synthetic/update-stage"))
        self.prepare.return_value = self.prepared

    def run_worker(self):
        self.worker.call_args.kwargs["target"]()

    def check_available(self):
        self.check.return_value = self.release
        self.panel.settings_shown()
        self.run_worker()
        self.panel._drain_events()

    def test_constructing_controls_never_checks_or_installs(self):
        self.check.assert_not_called()
        self.prepare.assert_not_called()
        self.ready.assert_not_called()
        self.assertFalse(self.panel.busy)
        self.assertIn("1.6.0", self.panel.status.get())

    def test_first_settings_show_starts_one_nonblocking_check(self):
        self.panel.settings_shown()
        self.panel.settings_shown()
        self.panel._clicked()
        self.assertTrue(self.panel.busy)
        self.worker.assert_called_once()
        self.check.assert_not_called()
        self.assertEqual(self.worker.call_args.kwargs["daemon"], True)
        self.run_worker()
        # Workers queue results; even a successful check does not modify Tk.
        self.assertIn("Checking", self.panel.status.get())
        self.panel._drain_events()
        self.check.assert_called_once_with("1.6.0")
        self.assertFalse(self.panel.busy)
        self.assertIn("Up to date", self.panel.status.get())

    def test_settings_reopens_are_throttled_but_manual_check_is_allowed(self):
        self.panel.settings_shown()
        self.run_worker()
        self.panel._drain_events()
        self.worker.reset_mock()
        self.clock.return_value = 1899
        self.panel.settings_shown()
        self.worker.assert_not_called()
        self.panel._clicked()
        self.worker.assert_called_once()
        self.run_worker()
        self.panel._drain_events()
        self.worker.reset_mock()
        self.clock.return_value = 2799
        self.panel.settings_shown()
        self.worker.assert_called_once()

    def test_new_release_requires_explicit_install_click(self):
        self.check_available()
        self.assertIn("Update v1.7.0 available", self.panel.status.get())
        self.panel.button.configure.assert_called_with(text="Install v1.7.0 & restart",
                                                       state="normal", style="Accent.TButton")
        self.prepare.assert_not_called()
        self.ready.assert_not_called()
        self.panel.settings_shown()
        self.prepare.assert_not_called()
        self.panel._clicked()
        self.assertTrue(self.panel.busy)
        self.run_worker()
        self.prepare.assert_called_once_with(self.release, progress=unittest.mock.ANY, cancelled=self.panel._cancel)
        self.ready.assert_not_called()
        self.panel._drain_events()
        self.ready.assert_called_once_with(self.prepared)

    def test_duplicate_ready_event_does_not_reinstall_or_discard_handoff(self):
        self.check_available()
        self.panel._clicked()
        generation = self.panel._generation
        self.run_worker()
        self.panel._publish("prepared", generation, self.prepared)
        self.panel._drain_events()
        self.panel._clicked()
        self.ready.assert_called_once_with(self.prepared)
        self.prepare.assert_called_once()
        self.discard.assert_not_called()

    def test_parent_can_close_synchronously_after_handoff_without_cleanup(self):
        self.check_available()
        self.ready.side_effect = lambda _job: self.panel.close()
        self.panel._clicked()
        self.run_worker()
        self.panel._drain_events()
        self.assertTrue(self.panel._closed)
        self.ready.assert_called_once_with(self.prepared)
        self.discard.assert_not_called()

    def test_source_run_reports_available_version_without_installing(self):
        self.support.return_value = (False, "Install the packaged app to use automatic updates.")
        self.check_available()
        self.assertIn("v1.7.0 available", self.panel.status.get())
        self.assertIn("packaged app", self.panel.status.get())
        self.panel.button.configure.assert_called_with(text="Check for updates", state="normal", style="TButton")
        self.panel._clicked()
        self.run_worker()
        self.panel._drain_events()
        self.prepare.assert_not_called()
        self.ready.assert_not_called()
        self.assertEqual(self.check.call_count, 2)

    def test_failed_check_can_retry_when_settings_reopens(self):
        self.check.side_effect = RuntimeError("Network unavailable")
        self.panel.settings_shown()
        self.run_worker()
        self.panel._drain_events()
        self.assertFalse(self.panel.busy)
        self.assertIn("Could not check", self.panel.status.get())
        self.panel.settings_shown()
        self.assertTrue(self.panel.busy)
        self.run_worker()
        self.assertEqual(self.check.call_count, 2)
        self.prepare.assert_not_called()

    def test_failed_download_leaves_install_retry_and_does_not_close_app(self):
        self.check_available()
        self.prepare.side_effect = RuntimeError("Checksum did not match")
        self.panel._clicked()
        self.run_worker()
        self.panel._drain_events()
        self.assertFalse(self.panel.busy)
        self.assertIn("Update was not installed", self.panel.status.get())
        self.panel.button.configure.assert_called_with(text="Install v1.7.0 & restart",
                                                       state="normal", style="Accent.TButton")
        self.ready.assert_not_called()
        self.panel._clicked()
        self.run_worker()
        self.assertEqual(self.prepare.call_count, 2)

    def test_install_support_is_rechecked_before_preparation(self):
        self.check_available()
        self.support.return_value = (False, "Installation changed")
        self.panel._clicked()
        self.run_worker()
        self.panel._drain_events()
        self.prepare.assert_not_called()
        self.ready.assert_not_called()
        self.assertIn("Installation changed", self.panel.status.get())

    def test_progress_is_queued_and_coalesced_by_percentage(self):
        self.check_available()
        def prepare(_release, *, progress, cancelled):
            self.assertFalse(cancelled.is_set())
            for amount in (10, 10, 10, 10, 500):
                progress(amount, 1000)
            self.assertIn("Downloading", self.panel.status.get())
            self.assertNotIn("50%", self.panel.status.get())
            return self.prepared
        self.prepare.side_effect = prepare
        self.panel._clicked()
        self.run_worker()
        events = list(self.panel._events.queue)
        self.assertEqual([value for kind, _, value in events if kind == "progress"], [1, 50])
        self.panel._drain_events()
        self.ready.assert_called_once()

    def test_close_cancels_and_discards_already_queued_prepared_job(self):
        self.check_available()
        self.panel._clicked()
        self.run_worker()
        self.assertTrue(self.panel.close())
        self.assertTrue(self.panel._cancel.is_set())
        self.discard.assert_called_once_with(self.prepared)
        self.ready.assert_not_called()
        self.panel._drain_events()
        self.ready.assert_not_called()

    def test_prepared_result_after_close_is_discarded_without_tk_calls(self):
        self.check_available()
        self.panel._clicked()
        generation = self.panel._generation
        self.panel.close()
        self.panel.button.reset_mock()
        self.panel._publish("prepared", generation, self.prepared)
        self.discard.assert_called_once_with(self.prepared)
        self.assertTrue(self.panel._events.empty())
        self.panel.button.configure.assert_not_called()
        self.ready.assert_not_called()

    def test_stale_prepared_result_is_cleaned_and_cannot_start_install(self):
        self.check_available()
        self.panel._publish("prepared", self.panel._generation - 1, self.prepared)
        self.discard.assert_called_once_with(self.prepared)
        self.panel._drain_events()
        self.ready.assert_not_called()

    def test_failed_parent_handoff_discards_job_and_shows_retry(self):
        self.check_available()
        self.ready.side_effect = RuntimeError("Could not save preferences")
        self.panel._clicked()
        self.run_worker()
        self.panel._drain_events()
        self.discard.assert_called_once_with(self.prepared)
        self.assertFalse(self.panel.busy)
        self.assertIn("Could not save preferences", self.panel.status.get())

    def test_cleanup_failure_is_retained_for_parent_shutdown(self):
        self.discard.side_effect = RuntimeError("Cannot remove staged download")
        self.panel._publish("prepared", self.panel._generation, self.prepared)
        self.assertFalse(self.panel.close())
        self.assertEqual(self.panel.cleanup_errors, ["Cannot remove staged download"])

    def test_failed_worker_start_returns_to_retry_state(self):
        self.worker.return_value.start.side_effect = RuntimeError("Worker unavailable")
        self.panel.settings_shown()
        self.panel._drain_events()
        self.assertFalse(self.panel.busy)
        self.assertIn("Worker unavailable", self.panel.status.get())
        self.check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
