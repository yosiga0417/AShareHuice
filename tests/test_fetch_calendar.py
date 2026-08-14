from __future__ import annotations

from datetime import date
import threading
import time
import unittest
from unittest.mock import patch

import pandas as pd

from backend import fetch


class TradeCalendarTests(unittest.TestCase):
    def tearDown(self) -> None:
        # Reset the module-level calendar state so tests stay isolated.
        with fetch.TRADE_CALENDAR_CONDITION:
            fetch.TRADE_CALENDAR_STATE.update({"fetched_at": 0.0, "days": None, "loading": False})

    def test_success_cached_within_ttl(self) -> None:
        df = pd.DataFrame({"trade_date": ["2024-01-02", "2024-01-03"]})
        calls = {"n": 0}

        def fake():
            calls["n"] += 1
            return df

        with patch.object(fetch.ak, "tool_trade_date_hist_sina", side_effect=fake):
            with patch.object(fetch, "TRADE_CALENDAR_TTL_SECONDS", 3600):
                days = fetch.load_trade_calendar()
                self.assertIsNotNone(days)
                assert days is not None
                self.assertIn(date(2024, 1, 2), days)
                self.assertIn(date(2024, 1, 3), days)
                self.assertIs(fetch.load_trade_calendar(), days)
        self.assertEqual(calls["n"], 1)

    def test_failure_cooldown_prevents_refetch_within_ttl(self) -> None:
        # Regression: a failed calendar fetch used to be retried on every call
        # (the TTL check only applied when days was cached), so a flaky source
        # was hit once per symbol batch. After the fix the failure itself gets
        # the TTL cooldown and callers fall back to the weekday heuristic.
        calls = {"n": 0}

        def fake():
            calls["n"] += 1
            raise RuntimeError("network down")

        with patch.object(fetch.ak, "tool_trade_date_hist_sina", side_effect=fake):
            with patch.object(fetch, "TRADE_CALENDAR_TTL_SECONDS", 3600):
                self.assertIsNone(fetch.load_trade_calendar())
                self.assertIsNone(fetch.load_trade_calendar())
                self.assertIsNone(fetch.load_trade_calendar())
        self.assertEqual(calls["n"], 1)

    def test_ttl_expiry_refetches(self) -> None:
        df = pd.DataFrame({"trade_date": ["2024-01-02"]})
        calls = {"n": 0}

        def fake():
            calls["n"] += 1
            return df

        with patch.object(fetch.ak, "tool_trade_date_hist_sina", side_effect=fake):
            with patch.object(fetch, "TRADE_CALENDAR_TTL_SECONDS", 3600):
                fetch.load_trade_calendar()
                # Simulate the TTL elapsing.
                with fetch.TRADE_CALENDAR_CONDITION:
                    fetch.TRADE_CALENDAR_STATE["fetched_at"] = 0.0
                fetch.load_trade_calendar()
        self.assertEqual(calls["n"], 2)

    def test_failure_then_recovery_fetches_again_after_cooldown(self) -> None:
        state = {"fail": True}
        calls = {"n": 0}
        df = pd.DataFrame({"trade_date": ["2024-01-02"]})

        def fake():
            calls["n"] += 1
            if state["fail"]:
                raise RuntimeError("network down")
            return df

        with patch.object(fetch.ak, "tool_trade_date_hist_sina", side_effect=fake):
            with patch.object(fetch, "TRADE_CALENDAR_TTL_SECONDS", 3600):
                self.assertIsNone(fetch.load_trade_calendar())
                # Cooldown: still failing, no refetch within TTL.
                self.assertIsNone(fetch.load_trade_calendar())
                # Source recovers and the cooldown expires -> refetch succeeds.
                state["fail"] = False
                with fetch.TRADE_CALENDAR_CONDITION:
                    fetch.TRADE_CALENDAR_STATE["fetched_at"] = 0.0
                days = fetch.load_trade_calendar()
        self.assertEqual(calls["n"], 2)
        self.assertIsNotNone(days)

    def _run_concurrent(self, n_threads: int, fn) -> list:
        results: list = []
        barrier = threading.Barrier(n_threads)
        lock = threading.Lock()

        def worker():
            barrier.wait()  # maximize the chance they all hit the loading window
            value = fn()
            with lock:
                results.append(value)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results

    def test_concurrent_first_load_waits_for_calendar(self) -> None:
        # Regression: on the first load, threads arriving while another thread
        # is mid-fetch used to fall back to "weekday == trading day" right
        # away, so only the loading thread treated an exchange holiday as a
        # non-trading day. Now they wait for the fetch result.
        # 2024-10-07 is a Monday inside the 国庆 holiday: weekday but NOT in
        # the calendar.
        holiday = date(2024, 10, 7)
        self.assertEqual(holiday.weekday(), 0)
        df = pd.DataFrame({"trade_date": ["2024-01-02", "2024-01-03", "2024-10-08"]})
        calls = {"n": 0}

        def fake():
            calls["n"] += 1
            time.sleep(0.2)  # keep the loading window open
            return df

        with patch.object(fetch.ak, "tool_trade_date_hist_sina", side_effect=fake):
            with patch.object(fetch, "TRADE_CALENDAR_TTL_SECONDS", 3600):
                results = self._run_concurrent(8, lambda: fetch.is_trading_day(holiday))
        self.assertEqual(calls["n"], 1)
        self.assertEqual(results, [False] * 8)

    def test_concurrent_first_load_failure_falls_back_to_weekday(self) -> None:
        # If the first load fails, waiting threads must still fall back to the
        # weekday heuristic (no deadlock, no refetch storm).
        holiday = date(2024, 10, 7)
        calls = {"n": 0}

        def fake():
            calls["n"] += 1
            time.sleep(0.2)
            raise RuntimeError("network down")

        with patch.object(fetch.ak, "tool_trade_date_hist_sina", side_effect=fake):
            with patch.object(fetch, "TRADE_CALENDAR_TTL_SECONDS", 3600):
                results = self._run_concurrent(8, lambda: fetch.is_trading_day(holiday))
        self.assertEqual(calls["n"], 1)
        self.assertEqual(results, [True] * 8)


if __name__ == "__main__":
    unittest.main()
