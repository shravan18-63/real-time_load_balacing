

from threading import Event, RLock

from pymongo.errors import PyMongoError

from ..models.simulation_model import list_active_simulation_ids
from .auth_service import AuthServiceError, get_current_user
from .simulation_service import (
    SimulationServiceError,
    control_simulation_for_user,
    get_simulation_for_user,
)


class SimulationRunner:
    def __init__(self, app, socketio):
        self.app = app
        self.socketio = socketio

        self.interval_seconds = 0.25
        self.step_seconds = 0.25

        self._lock = RLock()
        self._workers = {}
        self._shutdown = Event()

    def ensure_running(self, owner_id, simulation_id):
        key = (owner_id, simulation_id)

        with self._lock:
            if self._shutdown.is_set():
                raise RuntimeError("Simulation runner is shutting down.")

            if key in self._workers:
                return False

            marker = object()
            self._workers[key] = marker

            try:
                self.socketio.start_background_task(
                    self._run,
                    owner_id,
                    simulation_id,
                    marker,
                )
            except Exception:
                self._workers.pop(key, None)
                raise

        return True

    def recover(self):
        for owner_id, simulation_id in list_active_simulation_ids():
            self.ensure_running(owner_id, simulation_id)

    def shutdown(self):
        self._shutdown.set()

    def _publish(self, owner_id, state):
        try:
            self.app.extensions["simulation_namespace"].publish(
                owner_id,
                state,
            )
        except Exception:
            # Execution and persistence must survive a delivery failure.
            self.app.logger.exception(
                "Simulation update could not be delivered."
            )

    def _stop_after_error(self, owner_id, simulation_id):
        try:
            state = control_simulation_for_user(
                owner_id,
                simulation_id,
                "stop",
            )
            self._publish(owner_id, state)
        except Exception:
            self.app.logger.exception(
                "Could not stop simulation after worker failure."
            )

    def _run(self, owner_id, simulation_id, marker):
        key = (owner_id, simulation_id)

        try:
            with self.app.app_context():
                while not self._shutdown.is_set():
                    self.socketio.sleep(self.interval_seconds)

                    if self._shutdown.is_set():
                        break

                    try:
                        get_current_user(owner_id)

                        state = get_simulation_for_user(
                            owner_id,
                            simulation_id,
                        )

                        if state["status"] == "PAUSED":
                            continue

                        if state["status"] != "RUNNING":
                            break

                        state = control_simulation_for_user(
                            owner_id,
                            simulation_id,
                            "advance",
                            {"seconds": self.step_seconds},
                        )

                        self._publish(owner_id, state)

                        if state["status"] in {
                            "COMPLETED", "STOPPED", "FAILED"
                        }:
                            break

                    except AuthServiceError:
                        self.app.logger.warning(
                            "Stopping simulation because its account "
                            "is no longer available."
                        )
                        self._stop_after_error(owner_id, simulation_id)
                        break

                    except SimulationServiceError as exc:
                        if exc.status_code == 409:
                            # A simultaneous control changed the revision.
                            # Read the new state on the next iteration.
                            continue

                        if exc.status_code == 404:
                            break

                        self.app.logger.exception(
                            "Simulation worker could not advance."
                        )
                        self._stop_after_error(owner_id, simulation_id)
                        break

                    except PyMongoError:
                        self.app.logger.exception(
                            "Simulation waiting for database recovery."
                        )
                        self.socketio.sleep(1.0)

                    except Exception:
                        self.app.logger.exception(
                            "Unexpected simulation worker failure."
                        )
                        self._stop_after_error(owner_id, simulation_id)
                        break

        finally:
            with self._lock:
                if self._workers.get(key) is marker:
                    self._workers.pop(key, None)