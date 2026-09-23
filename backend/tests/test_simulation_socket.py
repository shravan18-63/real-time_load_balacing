import unittest

from copy import deepcopy
from unittest.mock import patch

from flask import Flask
from flask_jwt_extended import (
    JWTManager,
    create_access_token,
    create_refresh_token,
)
from flask_socketio import SocketIO

from app.services.auth_service import AuthServiceError
from app.services.simulation_service import SimulationServiceError
from app.sockets.simulation_socket import SimulationNamespace


MODULE = "app.sockets.simulation_socket"


class SimulationSocketTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="test-session-secret",
            JWT_SECRET_KEY="test-jwt-secret-" * 4,
        )

        JWTManager(self.app)
        self.socketio = SocketIO(
            self.app,
            async_mode="threading",
            logger=False,
            engineio_logger=False,
        )

        self.namespace = SimulationNamespace()
        self.socketio.on_namespace(self.namespace)
        self.clients = []

        self.states = {
            "sim-a": {
                "id": "sim-a",
                "revision": 0,
                "status": "CREATED",
            },
            "sim-b": {
                "id": "sim-b",
                "revision": 0,
                "status": "CREATED",
            },
        }

        self.user_mock = patch(
            f"{MODULE}.get_current_user",
            side_effect=lambda owner: {"id": owner},
        ).start()

        patch(
            f"{MODULE}.get_simulation_for_user",
            side_effect=self.lookup,
        ).start()

        self.addCleanup(patch.stopall)

    def tearDown(self):
        for client in self.clients:
            if client.is_connected():
                client.disconnect()

    def lookup(self, owner, simulation_id):
        ownership = {
            "sim-a": "owner-a",
            "sim-b": "owner-b",
        }

        if ownership.get(simulation_id) != owner:
            raise SimulationServiceError(
                "Simulation not found.",
                status_code=404,
            )

        return deepcopy(self.states[simulation_id])

    def connect(self, owner="owner-a"):
        with self.app.app_context():
            token = create_access_token(identity=owner)

        client = self.socketio.test_client(
            self.app,
            auth={"token": token},
        )
        self.clients.append(client)
        return client

    def subscribe(self, client, simulation_id="sim-a"):
        return client.emit(
            "subscribe_simulation",
            {"simulation_id": simulation_id},
            callback=True,
        )

    def test_missing_token_is_rejected(self):
        client = self.socketio.test_client(self.app)
        self.assertFalse(client.is_connected())

    def test_invalid_token_is_rejected(self):
        client = self.socketio.test_client(
            self.app,
            auth={"token": "not-a-valid-token"},
        )
        self.assertFalse(client.is_connected())

    def test_refresh_token_is_rejected(self):
        with self.app.app_context():
            token = create_refresh_token(identity="owner-a")

        client = self.socketio.test_client(
            self.app,
            auth={"token": token},
        )
        self.assertFalse(client.is_connected())

    def test_owner_receives_initial_snapshot(self):
        client = self.connect()
        response = self.subscribe(client)

        self.assertTrue(response["ok"])

        events = client.get_received()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["name"], "simulation_state")
        self.assertEqual(events[0]["args"][0]["id"], "sim-a")

    def test_other_users_simulation_is_rejected(self):
        client = self.connect("owner-b")
        response = self.subscribe(client, "sim-a")

        self.assertFalse(response["ok"])
        self.assertEqual(response["status"], 404)
        self.assertEqual(client.get_received(), [])

    def test_updates_are_private_and_older_revisions_are_ignored(self):
        owner = self.connect("owner-a")
        other = self.connect("owner-b")

        self.subscribe(owner, "sim-a")
        self.subscribe(other, "sim-b")
        owner.get_received()
        other.get_received()

        state = {
            "id": "sim-a",
            "revision": 2,
            "status": "RUNNING",
        }

        with self.app.app_context():
            self.namespace.publish("owner-a", state)
            self.namespace.publish(
                "owner-a",
                {**state, "revision": 1},
            )

        events = owner.get_received()

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["args"][0]["revision"], 2)
        self.assertEqual(other.get_received(), [])

    def test_expired_connection_is_disconnected_before_delivery(self):
        client = self.connect()
        self.subscribe(client)
        client.get_received()

        with self.app.app_context():
            with patch(f"{MODULE}.time", return_value=4_000_000_000):
                self.namespace.publish(
                    "owner-a",
                    {
                        "id": "sim-a",
                        "revision": 1,
                        "status": "RUNNING",
                    },
                )

        self.assertFalse(client.is_connected())

    def test_unsubscribe_stops_delivery(self):
        client = self.connect()
        self.subscribe(client)
        client.get_received()

        response = client.emit(
            "unsubscribe_simulation",
            callback=True,
        )
        self.assertTrue(response["ok"])

        with self.app.app_context():
            self.namespace.publish(
                "owner-a",
                {
                    "id": "sim-a",
                    "revision": 1,
                    "status": "RUNNING",
                },
            )

        self.assertEqual(client.get_received(), [])

    def test_reconnecting_receives_current_snapshot(self):
        first = self.connect()
        self.subscribe(first)
        first.disconnect()

        self.states["sim-a"]["revision"] = 7
        self.states["sim-a"]["status"] = "COMPLETED"

        second = self.connect()
        self.subscribe(second)

        event = second.get_received()[0]
        self.assertEqual(event["args"][0]["revision"], 7)
        self.assertEqual(event["args"][0]["status"], "COMPLETED")

    def test_invalid_subscription_payload_is_rejected(self):
        client = self.connect()

        response = client.emit(
            "subscribe_simulation",
            {"simulation_id": 123},
            callback=True,
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["status"], 400)

    def test_deleted_account_cannot_connect(self):
        self.user_mock.side_effect = AuthServiceError(
            "Account not found.",
            status_code=401,
        )

        client = self.connect()
        self.assertFalse(client.is_connected())


if __name__ == "__main__":
    unittest.main()