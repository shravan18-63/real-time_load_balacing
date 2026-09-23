from threading import RLock
from time import time

from flask import current_app, request
from flask_jwt_extended import decode_token
from flask_jwt_extended.exceptions import JWTExtendedException
from flask_socketio import Namespace
from jwt import PyJWTError
from pymongo.errors import PyMongoError

from ..services.auth_service import AuthServiceError, get_current_user
from ..services.simulation_service import (
    SimulationServiceError,
    get_simulation_for_user,
)


class SimulationNamespace(Namespace):
    def __init__(self):
        super().__init__("/")
        self._clients = {}
        self._lock = RLock()

    def on_connect(self, auth=None):
        if not isinstance(auth, dict):
            return False

        token = auth.get("token")
        if not isinstance(token, str) or not token:
            return False

        try:
            claims = decode_token(token)

            if claims.get("type") != "access":
                return False

            identity = claims.get("sub")
            expires_at = claims.get("exp")

            if not isinstance(identity, str):
                return False

            if (
                isinstance(expires_at, bool)
                or not isinstance(expires_at, (int, float))
                or expires_at <= time()
            ):
                return False

            user = get_current_user(identity)

        except (
            JWTExtendedException,
            PyJWTError,
            AuthServiceError,
            ValueError,
        ):
            return False

        except PyMongoError:
            current_app.logger.exception(
                "Socket authentication database error."
            )
            return False

        with self._lock:
            self._clients[request.sid] = {
                "owner_id": user["id"],
                "expires_at": expires_at,
                "simulation_id": None,
                "last_revision": -1,
            }

        return True

    def on_disconnect(self, reason=None):
        with self._lock:
            self._clients.pop(request.sid, None)

    def _reject_client(self, sid):
        with self._lock:
            self._clients.pop(sid, None)

            self.emit(
                "auth_error",
                {"error": "Authentication expired. Sign in again."},
                room=sid,
            )

            self.disconnect(sid)

    def _active_client(self, sid):
        with self._lock:
            client = self._clients.get(sid)
            client = dict(client) if client else None

        if client is None or client["expires_at"] <= time():
            self._reject_client(sid)
            raise AuthServiceError(
                "Authentication required.",
                status_code=401,
            )

        try:
            get_current_user(client["owner_id"])
        except AuthServiceError:
            self._reject_client(sid)
            raise

        return client

    def _send_state(self, sid, state):
        with self._lock:
            client = self._clients.get(sid)

            if client is None:
                return

            if client["expires_at"] <= time():
                self._reject_client(sid)
                return

            if client["simulation_id"] != state["id"]:
                return

            if state["revision"] <= client["last_revision"]:
                return

            self.emit(
                "simulation_state",
                state,
                room=sid,
            )

            client["last_revision"] = state["revision"]

    def on_subscribe_simulation(self, data=None):
        try:
            client = self._active_client(request.sid)

            if (
                not isinstance(data, dict)
                or not isinstance(data.get("simulation_id"), str)
            ):
                return {
                    "ok": False,
                    "status": 400,
                    "error": "Provide a simulation_id string.",
                }

            simulation_id = data["simulation_id"]

            # Check ownership before recording the subscription.
            get_simulation_for_user(
                client["owner_id"],
                simulation_id,
            )

            with self._lock:
                current = self._clients.get(request.sid)

                if current is None:
                    return {
                        "ok": False,
                        "status": 401,
                        "error": "Authentication required.",
                    }

                current["simulation_id"] = simulation_id
                current["last_revision"] = -1

            # Read again so updates during subscription are not missed.
            state = get_simulation_for_user(
                client["owner_id"],
                simulation_id,
            )

            self._send_state(request.sid, state)

            return {
                "ok": True,
                "simulation_id": simulation_id,
            }

        except (AuthServiceError, SimulationServiceError) as exc:
            return {
                "ok": False,
                "status": exc.status_code,
                "error": str(exc),
            }

        except PyMongoError:
            current_app.logger.exception(
                "Socket subscription database error."
            )
            return {
                "ok": False,
                "status": 503,
                "error": "Database temporarily unavailable.",
            }

        except Exception:
            current_app.logger.exception(
                "Unexpected simulation subscription failure."
            )
            return {
                "ok": False,
                "status": 500,
                "error": (
                    "Subscription failed on the server. "
                    "See the backend terminal for details."
                ),
            }

    def on_unsubscribe_simulation(self, data=None):
        with self._lock:
            client = self._clients.get(request.sid)

            if client:
                client["simulation_id"] = None
                client["last_revision"] = -1

        return {"ok": True}

    def publish(self, owner_id, state):
        with self._lock:
            recipients = [
                sid
                for sid, client in self._clients.items()
                if client["owner_id"] == owner_id
                and client["simulation_id"] == state["id"]
            ]

        if not recipients:
            return

        try:
            get_current_user(owner_id)
        except AuthServiceError:
            for sid in recipients:
                self._reject_client(sid)
            return

        for sid in recipients:
            self._send_state(sid, state)