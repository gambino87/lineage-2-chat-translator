"""Incoming translations must not wait for reply-language inference."""
import queue
import threading
import unittest
from unittest.mock import patch

import translator_app as app_module


class DeferredDetectionTests(unittest.TestCase):
    def test_incoming_and_cached_results_do_not_detect_language(self):
        app = app_module.TranslatorApp.__new__(app_module.TranslatorApp)
        app.stop = threading.Event()
        app.jobs, app.events = queue.Queue(), queue.Queue()
        app.config = {"ollama_url": app_module.ENDPOINT, "model": "test-model"}
        record = app_module.ChatRecord("12:00:00", "Sample", "Hola amigo", 0)
        for _ in range(2):
            app.jobs.put((record, "English", "Live", 0))
        original_put = app.events.put
        results = []

        def collect(event):
            original_put(event)
            if event[0] == "result":
                results.append(event[1])
                if len(results) == 2:
                    app.stop.set()

        app.events.put = collect
        with patch.object(app_module, "OllamaClient") as factory:
            factory.return_value.translate.return_value = "Hello friend"
            app._worker()
            factory.return_value.translate.assert_called_once()
            factory.return_value.detect_language.assert_not_called()
        self.assertEqual(len(results), 2)
        self.assertTrue(all(result[0].source_language is None for result in results))
        self.assertTrue(all(result[1] == "Hello friend" for result in results))
        self.assertEqual(app.jobs.unfinished_tasks, 0)

