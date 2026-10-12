"""Reconcile an accepted createRoom whose response was lost, using its exact alias."""

import io
import json
import tempfile
import threading
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from test_bridge import bridge


class CreateReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.instance = self.make_bridge()
        self.alias_path = "/_matrix/client/v3/directory/room/" + quote(
            self.instance.room_alias("lobby"), safe=""
        )
        self.room_id = "!created:matrix.example"

    def make_bridge(self):
        return bridge.Bridge(
            self.home,
            "matrix.example",
            "http://127.0.0.1:8080",
            "http://127.0.0.1:8008",
            "a" * 64,
            ["lobby"],
            0,
        )

    def error(self, code, status=400, retryable=False):
        return bridge.MatrixError(json.dumps({"errcode": code}), status, retryable)

    def http_error(self, code, status):
        return urllib.error.HTTPError(
            "http://127.0.0.1:8008",
            status,
            code,
            Message(),
            io.BytesIO(json.dumps({"errcode": code}).encode()),
        )

    def lost_response(self):
        return [
            self.http_error("M_NOT_FOUND", 404),
            urllib.error.URLError("create accepted; response connection lost"),
            self.http_error("M_ROOM_IN_USE", 400),
            io.BytesIO(json.dumps({"room_id": self.room_id}).encode()),
        ]

    def assert_unmapped(self):
        self.assertNotIn("lobby", self.instance.room_ids)
        self.assertNotIn("lobby", self.make_bridge().room_ids)

    def test_lost_create_response_relooks_up_identical_alias_and_persists(self):
        with (
            mock.patch.object(bridge.Bridge, "read_topic", return_value="topic"),
            mock.patch.object(bridge.time, "sleep"),
            mock.patch.object(
                bridge.urllib.request, "urlopen", side_effect=self.lost_response()
            ) as http,
        ):
            self.assertEqual(self.instance.ensure_room("lobby"), self.room_id)
        requests = [call.args[0] for call in http.call_args_list]
        self.assertEqual([req.method for req in requests], ["GET", "POST", "POST", "GET"])
        self.assertEqual(requests[0].full_url, self.instance.hs_url + self.alias_path)
        self.assertEqual(requests[3].full_url, requests[0].full_url)
        self.assertEqual(requests[1].data, requests[2].data)
        self.assertEqual(
            json.loads(requests[1].data)["room_alias_name"],
            self.instance.room_alias_localpart("lobby"),
        )
        restarted = self.make_bridge()
        with mock.patch.object(bridge, "matrix_request") as matrix:
            self.assertEqual(restarted.ensure_room("lobby"), self.room_id)
            matrix.assert_not_called()
        self.assertEqual(restarted.topics["lobby"], "topic")

    def test_outbound_loop_continues_and_persists_cursor_after_reconciliation(self):
        message = {"seq": 1, "from": "alice", "text": "hello"}
        view = {"first_seq": 1, "last_seq": 1, "messages": [message]}
        poll = bridge.poll_room_once
        calls = 0

        class StopProbe(BaseException):
            pass

        def bounded_poll(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise StopProbe()
            return poll(*args, **kwargs)

        with (
            mock.patch.object(bridge.Bridge, "read_topic", return_value=""),
            mock.patch.object(bridge.time, "sleep"),
            mock.patch.object(bridge.urllib.request, "urlopen", side_effect=self.lost_response()),
            mock.patch.object(bridge, "sync_topic"),
            mock.patch.object(bridge, "technocore_read_room", return_value=view),
            mock.patch.object(bridge, "deliver_message") as deliver,
            mock.patch.object(bridge, "poll_room_once", side_effect=bounded_poll),
        ):
            with self.assertRaises(StopProbe):
                bridge.outbound_loop(self.instance, "lobby")
        deliver.assert_called_once_with(self.instance, "lobby", self.room_id, message)
        restarted = self.make_bridge()
        self.assertEqual(restarted.room_ids["lobby"], self.room_id)
        self.assertEqual(restarted.cursors["lobby"], 1)
        self.assertEqual(restarted.checkpoints["lobby"], bridge.record_checkpoint(message))

    def test_failed_relookup_does_not_persist_a_room(self):
        for failure in (
            self.error("M_NOT_FOUND", 404),
            self.error("M_FORBIDDEN", 403),
            self.error("M_UNAVAILABLE", 503, True),
        ):
            with (
                self.subTest(error=str(failure)),
                mock.patch.object(bridge.Bridge, "read_topic", return_value=""),
                mock.patch.object(
                    bridge,
                    "matrix_request",
                    side_effect=[
                        self.error("M_NOT_FOUND", 404),
                        self.error("M_ROOM_IN_USE"),
                        failure,
                    ],
                ) as matrix,
            ):
                with self.assertRaises(bridge.MatrixError) as raised:
                    self.instance.ensure_room("lobby")
                self.assertIs(raised.exception, failure)
                self.assertEqual(matrix.call_count, 3)
                self.assert_unmapped()

    def test_invalid_relookup_room_ids_are_rejected(self):
        for room_id in (None, "", "room:matrix.example", 42, [], {}):
            with (
                self.subTest(room_id=room_id),
                mock.patch.object(bridge.Bridge, "read_topic", return_value=""),
                mock.patch.object(
                    bridge,
                    "matrix_request",
                    side_effect=[
                        self.error("M_NOT_FOUND", 404),
                        self.error("M_ROOM_IN_USE"),
                        {"room_id": room_id},
                    ],
                ),
            ):
                with self.assertRaisesRegex(bridge.MatrixError, "invalid room_id"):
                    self.instance.ensure_room("lobby")
                self.assert_unmapped()

    def test_other_create_failures_are_not_reconciled(self):
        for failure in (
            self.error("M_FORBIDDEN", 400),
            self.error("M_ROOM_IN_USE", 403),
            self.error("M_ROOM_IN_USE", 409),
            self.error("M_ROOM_IN_USE", 503, True),
        ):
            with (
                self.subTest(error=(failure.status, str(failure))),
                mock.patch.object(bridge.Bridge, "read_topic", return_value=""),
                mock.patch.object(
                    bridge,
                    "matrix_request",
                    side_effect=[
                        self.error("M_NOT_FOUND", 404),
                        failure,
                    ],
                ) as matrix,
            ):
                with self.assertRaises(bridge.MatrixError) as raised:
                    self.instance.ensure_room("lobby")
                self.assertIs(raised.exception, failure)
                self.assertEqual(matrix.call_count, 2)
                self.assert_unmapped()

    def test_initial_alias_permission_failure_does_not_create_or_relookup(self):
        failure = self.error("M_FORBIDDEN", 403)
        with (
            mock.patch.object(bridge.Bridge, "read_topic", return_value=""),
            mock.patch.object(bridge, "matrix_request", side_effect=failure) as matrix,
        ):
            with self.assertRaises(bridge.MatrixError) as raised:
                self.instance.ensure_room("lobby")
        self.assertIs(raised.exception, failure)
        self.assertEqual(matrix.call_count, 1)
        self.assert_unmapped()

    def test_existing_alias_is_used_without_create(self):
        with (
            mock.patch.object(bridge.Bridge, "read_topic", return_value=""),
            mock.patch.object(
                bridge, "matrix_request", return_value={"room_id": self.room_id}
            ) as matrix,
        ):
            self.assertEqual(self.instance.ensure_room("lobby"), self.room_id)
        self.assertEqual(matrix.call_count, 1)
        self.assertEqual(matrix.call_args.args[1:3], ("GET", self.alias_path))

    def test_successful_first_create_does_not_relookup(self):
        with (
            mock.patch.object(bridge.Bridge, "read_topic", return_value=""),
            mock.patch.object(
                bridge,
                "matrix_request",
                side_effect=[
                    self.error("M_NOT_FOUND", 404),
                    {"room_id": self.room_id},
                ],
            ) as matrix,
        ):
            self.assertEqual(self.instance.ensure_room("lobby"), self.room_id)
        self.assertEqual([call.args[1] for call in matrix.call_args_list], ["GET", "POST"])

    def test_concurrent_ensure_reuses_reconciled_room(self):
        start = threading.Barrier(2)

        def ensure():
            start.wait(timeout=3)
            return self.instance.ensure_room("lobby")

        with (
            mock.patch.object(bridge.Bridge, "read_topic", return_value=""),
            mock.patch.object(
                bridge,
                "matrix_request",
                side_effect=[
                    self.error("M_NOT_FOUND", 404),
                    self.error("M_ROOM_IN_USE"),
                    {"room_id": self.room_id},
                ],
            ) as matrix,
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            futures = [pool.submit(ensure) for _ in range(2)]
            self.assertEqual([f.result(timeout=3) for f in futures], [self.room_id, self.room_id])
        self.assertEqual(matrix.call_count, 3)
