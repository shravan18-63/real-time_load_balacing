import unittest

from unittest.mock import MagicMock, patch

from flask import Flask
from pymongo.errors import ConnectionFailure

from app.services.simulation_runner import SimulationRunner
from app.services.simulation_service import SimulationServiceError


MODULE = "app.services.simulation_runner"


class SimulationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["TESTING"] = True
        self.app.logger.disabled = True

        self.socketio = MagicMock()
        self.publisher = MagicMock()
        self.app.extensions["simulation_namespace"] = self.publisher

        self.runner = SimulationRunner(self.app, self.socketio)

        self.user_lookup = patch(
            f"{MODULE}.get_current_user",
            return_value={"id": "owner"},
        ).start()

        self.get_state = patch(
            f"{MODULE}.get_simulation_for_user",
        ).start()

        self.control = patch(
            f"{MODULE}.control_simulation_for_user",
        ).start()

        self.addCleanup(patch.stopall)

    def tearDown(self):
        self.runner.shutdown()
        self.app.logger.disabled = False

    def run_scheduled_worker(self):
        call = self.socketio.start_background_task.call_args
        target = call.args[0]
        target(*call.args[1:])

    def test_duplicate_workers_are_prevented(self):
        self.assertTrue(
            self.runner.ensure_running("owner", "simulation")
        )
        self.assertFalse(
            self.runner.ensure_running("owner", "simulation")
        )

        self.socketio.start_background_task.assert_called_once()

    def test_worker_advances_and_publishes_completion(self):
        self.get_state.return_value = {"status": "RUNNING"}
        completed = {
            "id": "simulation",
            "status": "COMPLETED",
            "revision": 1,
        }
        self.control.return_value = completed

        self.runner.ensure_running("owner", "simulation")
        self.run_scheduled_worker()

        self.control.assert_called_once_with(
            "owner",
            "simulation",
            "advance",
            {"seconds": 0.25},
        )
        self.publisher.publish.assert_called_once_with(
            "owner",
            completed,
        )
        self.assertEqual(self.runner._workers, {})

    def test_paused_run_waits_until_resumed(self):
        self.get_state.side_effect = [
            {"status": "PAUSED"},
            {"status": "RUNNING"},
        ]
        self.control.return_value = {
            "id": "simulation",
            "status": "COMPLETED",
        }

        self.runner.ensure_running("owner", "simulation")
        self.run_scheduled_worker()

        self.assertEqual(self.get_state.call_count, 2)
        self.control.assert_called_once()

    def test_stopped_run_is_not_advanced(self):
        self.get_state.return_value = {"status": "STOPPED"}

        self.runner.ensure_running("owner", "simulation")
        self.run_scheduled_worker()

        self.control.assert_not_called()
        self.assertEqual(self.runner._workers, {})

    def test_revision_conflict_retries_with_current_state(self):
        self.get_state.return_value = {"status": "RUNNING"}
        self.control.side_effect = [
            SimulationServiceError("Conflict.", status_code=409),
            {"id": "simulation", "status": "COMPLETED"},
        ]

        self.runner.ensure_running("owner", "simulation")
        self.run_scheduled_worker()

        self.assertEqual(self.control.call_count, 2)
        self.publisher.publish.assert_called_once()

    def test_database_outage_does_not_advance_time(self):
        self.get_state.side_effect = [
            ConnectionFailure("Database unavailable."),
            {"status": "RUNNING"},
        ]
        self.control.return_value = {
            "id": "simulation",
            "status": "COMPLETED",
        }

        self.runner.ensure_running("owner", "simulation")
        self.run_scheduled_worker()

        self.control.assert_called_once()
        self.socketio.sleep.assert_any_call(1.0)

    def test_delivery_failure_does_not_undo_execution(self):
        self.get_state.return_value = {"status": "RUNNING"}
        self.control.return_value = {
            "id": "simulation",
            "status": "COMPLETED",
        }
        self.publisher.publish.side_effect = RuntimeError(
            "Delivery failed."
        )

        self.runner.ensure_running("owner", "simulation")
        self.run_scheduled_worker()

        self.control.assert_called_once()
        self.assertEqual(self.runner._workers, {})

    def test_launch_failure_releases_worker_registration(self):
        self.socketio.start_background_task.side_effect = RuntimeError(
            "Cannot launch worker."
        )

        with self.assertRaises(RuntimeError):
            self.runner.ensure_running("owner", "simulation")

        self.assertEqual(self.runner._workers, {})

    def test_recovery_registers_saved_active_runs(self):
        with patch(
            f"{MODULE}.list_active_simulation_ids",
            return_value=[
                ("owner", "first"),
                ("owner", "second"),
            ],
        ):
            self.runner.recover()

        self.assertEqual(
            self.socketio.start_background_task.call_count,
            2,
        )

    def test_shutdown_prevents_new_workers(self):
        self.runner.shutdown()

        with self.assertRaises(RuntimeError):
            self.runner.ensure_running("owner", "simulation")


if __name__ == "__main__":
    unittest.main()