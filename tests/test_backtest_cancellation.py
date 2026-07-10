from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from backend import api


class BacktestCancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        with api.BACKTEST_TASKS_LOCK:
            api.BACKTEST_TASKS.clear()
        with api.ACTIVE_TASK_COUNT_LOCK:
            api.ACTIVE_TASK_COUNT = 0

    def tearDown(self) -> None:
        with api.BACKTEST_TASKS_LOCK:
            api.BACKTEST_TASKS.clear()
        with api.ACTIVE_TASK_COUNT_LOCK:
            api.ACTIVE_TASK_COUNT = 0

    def test_cancel_request_remains_cancelling_until_worker_exits(self) -> None:
        task = api.create_backtest_task()
        worker_entered = threading.Event()
        release_worker = threading.Event()

        def blocked_execute(*_args: object, **_kwargs: object) -> dict[str, object]:
            worker_entered.set()
            release_worker.wait(timeout=2)
            raise api.BacktestCancelled("回测已取消")

        with api.ACTIVE_TASK_COUNT_LOCK:
            api.ACTIVE_TASK_COUNT = 1

        worker = threading.Thread(target=api.run_backtest_task, args=(task.task_id, object()))
        with patch("backend.api.execute_backtest", side_effect=blocked_execute):
            worker.start()
            try:
                self.assertTrue(worker_entered.wait(timeout=1))
                api.request_backtest_cancel(task.task_id)
                pending = api.get_backtest_task_payload(task.task_id)

                self.assertEqual(pending["status"], "cancelling")
                self.assertTrue(pending["cancel_requested"])
                self.assertIsNone(task.finished_monotonic)
                with api.ACTIVE_TASK_COUNT_LOCK:
                    self.assertEqual(api.ACTIVE_TASK_COUNT, 1)
            finally:
                release_worker.set()
                worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        completed = api.get_backtest_task_payload(task.task_id)

        self.assertEqual(completed["status"], "cancelled")
        self.assertIsNotNone(task.finished_monotonic)
        with api.ACTIVE_TASK_COUNT_LOCK:
            self.assertEqual(api.ACTIVE_TASK_COUNT, 0)

    def test_completed_task_cannot_be_changed_to_cancelled(self) -> None:
        task = api.create_backtest_task()
        api.update_backtest_task(task.task_id, status="completed", stage="completed")

        returned = api.request_backtest_cancel(task.task_id)

        self.assertEqual(returned.status, "completed")
        self.assertFalse(returned.cancel_requested)

if __name__ == "__main__":
    unittest.main()
