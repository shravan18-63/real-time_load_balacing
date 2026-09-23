import { useCallback, useEffect, useState } from "react";
import { io } from "socket.io-client";

import { useAuth } from "../context/AuthContext.jsx";
import api from "../services/api.js";

export default function useSimulationSocket(simulationId) {
  const { accessToken, logout } = useAuth();

  const [snapshot, setSnapshot] = useState(null);
  const [connectionStatus, setConnectionStatus] = useState("idle");
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);

  const reconnect = useCallback(() => {
    setAttempt((value) => value + 1);
  }, []);

  useEffect(() => {
    setSnapshot(null);
    setError("");

    if (!simulationId || !accessToken) {
      setConnectionStatus("idle");
      return;
    }

    let active = true;
    let lastRevision = -1;

    const serverURL = new URL(
      api.defaults.baseURL,
      window.location.origin
    ).origin;

    const socket = io(serverURL, {
      autoConnect: false,
      forceNew: true,
      auth: { token: accessToken },
      reconnection: true,
      reconnectionDelay: 1000,
      reconnectionDelayMax: 5000,
      timeout: 10000,
    });

    function handleConnect() {
      if (!active) return;

      setConnectionStatus("subscribing");
      setError("");

      socket.timeout(10000).emit(
        "subscribe_simulation",
        { simulation_id: simulationId },
        (timeoutError, response) => {
          if (!active) return;

          if (timeoutError) {
            setConnectionStatus("error");
            setError("Subscription timed out. Try reconnecting.");
            return;
          }

          if (!response?.ok) {
            setConnectionStatus("error");
            setError(response?.error || "Subscription failed.");

            if (response?.status === 401) {
              logout();
            }
            return;
          }

          setConnectionStatus("connected");
        }
      );
    }

    function handleState(state) {
      if (
        !active ||
        state?.id !== simulationId ||
        !Number.isInteger(state.revision) ||
        state.revision <= lastRevision
      ) {
        return;
      }

      lastRevision = state.revision;
      setSnapshot(state);
    }

    function handleDisconnect() {
      if (active) {
        setConnectionStatus("disconnected");
      }
    }

    function handleConnectError(connectionError) {
      if (!active) return;

      setConnectionStatus("error");
      setError(
        connectionError.message || "Unable to connect to live updates."
      );
    }

    function handleAuthError() {
      if (!active) return;

      setError("Your session expired. Sign in again.");
      logout();
    }

    function handleReconnectAttempt() {
      if (active) {
        setConnectionStatus("reconnecting");
      }
    }

    socket.on("connect", handleConnect);
    socket.on("simulation_state", handleState);
    socket.on("disconnect", handleDisconnect);
    socket.on("connect_error", handleConnectError);
    socket.on("auth_error", handleAuthError);
    socket.io.on("reconnect_attempt", handleReconnectAttempt);

    setConnectionStatus("connecting");
    socket.connect();

    return () => {
      active = false;
      socket.removeAllListeners();
      socket.io.off("reconnect_attempt", handleReconnectAttempt);
      socket.disconnect();
    };
  }, [simulationId, accessToken, logout, attempt]);

  return {
    snapshot: snapshot?.id === simulationId ? snapshot : null,
    connectionStatus,
    error,
    reconnect,
  };
}