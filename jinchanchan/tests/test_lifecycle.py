# -*- coding: utf-8 -*-

import asyncio
import json
import os
import tempfile
import unittest

from agent.lifecycle import (
    EXIT_STOPPED,
    EXIT_TIMEOUT,
    EventWriter,
    LifecycleGuard,
    RunAbort,
    StableMatchReadyGate,
    is_valid_round,
    parse_device_id,
    parse_match_result,
    resolve_device_endpoint,
)


class FakeBanner:
    def __init__(self, samples):
        self.samples = list(samples)

    async def detect(self):
        if self.samples:
            return self.samples.pop(0)
        return None, None


class LifecycleTests(unittest.TestCase):
    def test_device_id_and_conflict(self):
        endpoint = parse_device_id("127.0.0.1:5555")
        self.assertEqual(endpoint.host, "127.0.0.1")
        self.assertEqual(endpoint.port, 5555)
        self.assertEqual(endpoint.device_id, "127.0.0.1:5555")

        with self.assertRaises(ValueError):
            resolve_device_endpoint("127.0.0.1:5555", "localhost", None)
        with self.assertRaises(ValueError):
            parse_device_id("emulator-5554")

    def test_event_writer_contract_and_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            event_file = os.path.join(temp_dir, "events.jsonl")
            events = EventWriter(event_file, run_id="run-001")
            self.assertTrue(events.emit("MATCH_READY", once=True, round="1-1"))
            self.assertFalse(events.emit("MATCH_READY", once=True))
            events.emit("SCRIPT_STOPPED", reason="match_finished")

            with open(event_file, encoding="utf-8") as handle:
                records = [json.loads(line) for line in handle if line.strip()]

            self.assertEqual([1, 2], [item["seq"] for item in records])
            self.assertEqual({"run-001"}, {item["run_id"] for item in records})
            self.assertEqual(1, records[0]["schema_version"])
            self.assertEqual("MATCH_READY", records[0]["event"])
            self.assertEqual("SCRIPT_STOPPED", records[1]["event"])

    def test_lifecycle_stop_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            stop_file = os.path.join(temp_dir, "stop.game")

            async def run_test():
                guard = LifecycleGuard(stop_file, timeout=5, poll_interval=0.01)
                task = asyncio.create_task(guard.wait_for_abort())
                await asyncio.sleep(0.03)
                with open(stop_file, "w", encoding="utf-8"):
                    pass
                return await task

            signal = asyncio.run(run_test())
            self.assertEqual(EXIT_STOPPED, signal.code)
            self.assertEqual("stop_file", signal.reason)

    def test_lifecycle_timeout(self):
        async def run_test():
            guard = LifecycleGuard(timeout=0.02, poll_interval=0.01)
            return await guard.wait_for_abort()

        signal = asyncio.run(run_test())
        self.assertEqual(EXIT_TIMEOUT, signal.code)
        self.assertEqual("timeout", signal.reason)

    def test_strict_match_ready_requires_two_samples(self):
        async def run_test():
            gate = StableMatchReadyGate(
                FakeBanner([(None, None), ("battle", "1-1"), ("battle", "1-1")]),
                samples=2,
                interval=0,
                timeout=1,
            )
            return await gate.wait()

        self.assertEqual(("battle", "1-1"), asyncio.run(run_test()))
        self.assertTrue(is_valid_round("7-7"))
        self.assertFalse(is_valid_round("8-1"))
        self.assertFalse(is_valid_round("2-9"))

    def test_match_ready_timeout(self):
        async def run_test():
            gate = StableMatchReadyGate(
                FakeBanner([("battle", "1-1")]),
                samples=2,
                interval=0,
                timeout=0.02,
            )
            await gate.wait()

        with self.assertRaises(RunAbort) as caught:
            asyncio.run(run_test())
        self.assertEqual(EXIT_TIMEOUT, caught.exception.code)
        self.assertEqual("match_ready", caught.exception.stage)

    def test_match_result(self):
        self.assertEqual(("win", 1), parse_match_result(["您获得了第一名"]))
        self.assertEqual(("loss", 8), parse_match_result(["第八名"]))
        self.assertEqual(("unknown", None), parse_match_result(["战绩回顾"]))


if __name__ == "__main__":
    unittest.main()
