# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Slow-request logging keeps performance diagnostics free of URL secrets."""

import logging
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app as appModule
from _app_factory import AppTestCase


SLOW_REQUEST_LOG_THRESHOLD_SECONDS = 1.0
START_TIME = 100.0
ZERO_START_TIME = 0.0
FAST_DURATION_SECONDS = 0.25
EXACT_THRESHOLD_DURATION_SECONDS = SLOW_REQUEST_LOG_THRESHOLD_SECONDS
OVER_THRESHOLD_DURATION_SECONDS = SLOW_REQUEST_LOG_THRESHOLD_SECONDS + 0.001
SHARE_TOKEN = "secret-share-token"
IMAGE_FILENAME = "cover.jpeg"
SLOW_REQUEST_LOG_MARKER = "Slow request"


class SlowRequestLoggingTestCase(AppTestCase):
    def _slowRequestLogs(self, records):
        return [record for record in records
                if record.getMessage().startswith(SLOW_REQUEST_LOG_MARKER)]

    def _makeEndpoint(self, dash, path, view, methods=None):
        dash.app.add_url_rule(path, endpoint=path.strip("/") or "root",
                              view_func=view, methods=methods)

    def test_fast_request_is_silent(self):
        dash = self._makeApp()
        client = dash.app.test_client()

        with patch.object(appModule.time, "monotonic",
                          side_effect=(START_TIME, START_TIME + FAST_DURATION_SECONDS)), \
             self.assertNoLogs("app", level=logging.WARNING):
            client.get("/health")

    def test_exact_threshold_request_is_silent(self):
        dash = self._makeApp()
        client = dash.app.test_client()

        with patch.object(appModule.time, "monotonic",
                          side_effect=(START_TIME, START_TIME + EXACT_THRESHOLD_DURATION_SECONDS)), \
             self.assertNoLogs("app", level=logging.WARNING):
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)

    def test_over_threshold_request_logs_once_with_method_route_and_duration(self):
        dash = self._makeApp()
        client = dash.app.test_client()

        with patch.object(appModule.time, "monotonic",
                          side_effect=(START_TIME, START_TIME + OVER_THRESHOLD_DURATION_SECONDS)), \
             self.assertLogs("app", level=logging.WARNING) as captured:
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        slowLogs = self._slowRequestLogs(captured.records)
        self.assertEqual(len(slowLogs), 1)
        message = slowLogs[0].getMessage()
        self.assertIn("method=GET", message)
        self.assertIn("route=/health", message)
        self.assertIn("duration=1.001s", message)

    def test_zero_start_timestamp_is_valid(self):
        dash = self._makeApp()
        client = dash.app.test_client()

        with patch.object(appModule.time, "monotonic",
                          side_effect=(ZERO_START_TIME, OVER_THRESHOLD_DURATION_SECONDS)), \
             self.assertLogs("app", level=logging.WARNING) as captured:
            response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self._slowRequestLogs(captured.records)), 1)

    def test_shared_page_logs_template_without_token(self):
        dash = self._makeApp()
        client = dash.app.test_client()

        with patch.object(appModule.time, "monotonic",
                          side_effect=(START_TIME, START_TIME + OVER_THRESHOLD_DURATION_SECONDS)), \
             patch.object(dash.repo, "isShareLinksEnabled", return_value=False), \
             self.assertLogs("app", level=logging.WARNING) as captured:
            client.get(f"/shared/{SHARE_TOKEN}")

        slowLogs = self._slowRequestLogs(captured.records)
        self.assertEqual(len(slowLogs), 1)
        message = slowLogs[0].getMessage()
        self.assertIn("route=/shared/<token>", message)
        self.assertNotIn(SHARE_TOKEN, message)

    def test_shared_image_routes_log_templates_without_token(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        pathsAndTemplates = (
            (f"/shared/{SHARE_TOKEN}/img/tracks/{IMAGE_FILENAME}",
             "/shared/<token>/img/tracks/<filename>"),
            (f"/shared/{SHARE_TOKEN}/img/artists/{IMAGE_FILENAME}",
             "/shared/<token>/img/artists/<filename>"),
        )

        for path, routeTemplate in pathsAndTemplates:
            with patch.object(appModule.time, "monotonic",
                              side_effect=(START_TIME, START_TIME + OVER_THRESHOLD_DURATION_SECONDS)), \
                 patch.object(dash.repo, "isShareLinksEnabled", return_value=False), \
                 self.assertLogs("app", level=logging.WARNING) as captured:
                client.get(path)

            slowLogs = self._slowRequestLogs(captured.records)
            self.assertEqual(len(slowLogs), 1)
            message = slowLogs[0].getMessage()
            self.assertIn(f"route={routeTemplate}", message)
            self.assertNotIn(SHARE_TOKEN, message)

    def test_handled_error_logs_once(self):
        dash = self._makeApp()

        def handledError():
            raise ValueError("expected handled test exception")

        @dash.app.errorhandler(ValueError)
        def handleValueError(error):
            return "handled", 500

        self._makeEndpoint(dash, "/handled-error", handledError)

        with patch.object(appModule.time, "monotonic",
                          side_effect=(START_TIME, START_TIME + OVER_THRESHOLD_DURATION_SECONDS)), \
             self.assertLogs("app", level=logging.WARNING) as captured:
            response = dash.app.test_client().get("/handled-error")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(len(self._slowRequestLogs(captured.records)), 1)

    def test_propagated_exception_logs_once(self):
        dash = self._makeApp()
        dash.app.config["PROPAGATE_EXCEPTIONS"] = True

        def propagatedError():
            raise RuntimeError("expected test exception")

        self._makeEndpoint(dash, "/propagated-error", propagatedError)

        with patch.object(appModule.time, "monotonic",
                          side_effect=(START_TIME, START_TIME + OVER_THRESHOLD_DURATION_SECONDS)), \
             self.assertLogs("app", level=logging.WARNING) as captured:
            with self.assertRaises(RuntimeError):
                dash.app.test_client().get("/propagated-error")

        self.assertEqual(len(self._slowRequestLogs(captured.records)), 1)

    def test_unmatched_route_logs_fixed_marker_without_path(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        unmatchedPath = f"/missing/{SHARE_TOKEN}?query=private"

        with patch.object(appModule.time, "monotonic",
                          side_effect=(START_TIME, START_TIME + OVER_THRESHOLD_DURATION_SECONDS)), \
             self.assertLogs("app", level=logging.WARNING) as captured:
            response = client.get(unmatchedPath)

        self.assertEqual(response.status_code, 404)
        slowLogs = self._slowRequestLogs(captured.records)
        self.assertEqual(len(slowLogs), 1)
        message = slowLogs[0].getMessage()
        self.assertIn("route=<unmatched>", message)
        self.assertNotIn(SHARE_TOKEN, message)
        self.assertNotIn("private", message)

    def test_slow_csrf_failure_is_logged(self):
        dash = self._makeApp()
        dash.app.config["WTF_CSRF_ENABLED"] = True

        def csrfProtectedEndpoint():
            return "unexpected"

        self._makeEndpoint(dash, "/csrf-failure", csrfProtectedEndpoint, methods=["POST"])

        with patch.object(appModule.time, "monotonic",
                          side_effect=(START_TIME, START_TIME + OVER_THRESHOLD_DURATION_SECONDS)), \
             self.assertLogs("app", level=logging.WARNING) as captured:
            response = dash.app.test_client().post("/csrf-failure")

        self.assertEqual(response.status_code, 400)
        slowLogs = self._slowRequestLogs(captured.records)
        self.assertEqual(len(slowLogs), 1)
        self.assertIn("route=/csrf-failure", slowLogs[0].getMessage())

    def test_teardown_without_start_timestamp_is_silent_and_safe(self):
        dash = self._makeApp()

        def abortBeforeStamp():
            return "early", 400

        self._makeEndpoint(dash, "/before-stamp", abortBeforeStamp)

        def abortingBeforeRequest():
            from flask import abort
            abort(400)

        dash.app.before_request_funcs.setdefault(None, []).insert(0, abortingBeforeRequest)

        with self.assertNoLogs("app", level=logging.WARNING):
            response = dash.app.test_client().get("/before-stamp")

        self.assertEqual(response.status_code, 400)

    def test_preserved_request_context_does_not_duplicate_teardown_log(self):
        dash = self._makeApp()
        client = dash.app.test_client()

        with patch.object(appModule.time, "monotonic",
                          side_effect=(START_TIME, START_TIME + OVER_THRESHOLD_DURATION_SECONDS)), \
             self.assertLogs("app", level=logging.WARNING) as captured:
            with client:
                response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self._slowRequestLogs(captured.records)), 1)


if __name__ == "__main__":
    unittest.main()
