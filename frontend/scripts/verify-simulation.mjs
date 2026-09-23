import assert from "node:assert/strict";
import { setTimeout as delay } from "node:timers/promises";
import { io } from "socket.io-client";

const BASE = process.env.SIM_TEST_URL || "http://127.0.0.1:5000";
const email = process.env.SIM_TEST_EMAIL;
const password = process.env.SIM_TEST_PASSWORD;

let token;
let socket;
let taskId;
let vmId;
let selectedId;
let latest = null;
let socketError = null;
let stage = "initialization";

const simulationIds = [];

function beginStage(description) {
  stage = description;
  console.log(`CHECK: ${description}`);
}

async function request(path, method = "GET", body) {
  for (let attempt = 0; attempt < 5; attempt++) {
    let response;
    let data;

    try {
      response = await fetch(`${BASE}/api${path}`, {
        method,
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: AbortSignal.timeout(10000),
      });

      if (response.status === 204) return null;

      data = await response.json();
    } catch (error) {
      throw new Error(
        `${method} ${path}: ${error.message}`,
        { cause: error }
      );
    }

    // Retry when a worker saves a newer revision during a control request.
    if (response.status === 409 && attempt < 4) {
      await delay(100);
      continue;
    }

    if (!response.ok) {
      throw new Error(
        `${method} ${path}: ${data.error || data.msg || response.status}`
      );
    }

    return data;
  }

  throw new Error(`${method} ${path}: request retries exhausted.`);
}

async function waitFor(predicate, description, timeout = 15000) {
  const deadline = Date.now() + timeout;

  while (Date.now() < deadline) {
    if (socketError) throw socketError;
    if (predicate()) return;
    await delay(100);
  }

  throw new Error(
    `Timed out waiting for ${description}. ` +
    `Socket connected: ${socket?.connected ?? false}. ` +
    `Latest status: ${latest?.status ?? "none"}. ` +
    `Latest revision: ${latest?.revision ?? "none"}.`
  );
}

async function connectSocket() {
  socketError = null;

  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      removeListeners();
      reject(new Error("WebSocket connection timed out."));
    }, 10000);

    function removeListeners() {
      clearTimeout(timer);
      socket.off("connect", connected);
      socket.off("connect_error", failed);
    }

    function connected() {
      removeListeners();
      console.log(`Socket connected: ${socket.id}`);
      resolve();
    }

    function failed(error) {
      removeListeners();
      reject(
        new Error(
          `WebSocket connection failed: ${error.message}`,
          { cause: error }
        )
      );
    }

    socket.once("connect", connected);
    socket.once("connect_error", failed);
    socket.connect();
  });
}

async function subscribe(simulationId) {
  selectedId = simulationId;
  latest = null;

  assert(socket.connected, "Socket disconnected before subscription.");
  console.log(`Subscribing to simulation ${simulationId}...`);

  await new Promise((resolve, reject) => {
    socket.timeout(10000).emit(
      "subscribe_simulation",
      { simulation_id: simulationId },
      (error, reply) => {
        if (error) {
          return reject(
            new Error(
              "Subscription acknowledgement timed out. " +
              `Socket connected: ${socket.connected}. ` +
              "Check the backend terminal for the subscription error.",
              { cause: error }
            )
          );
        }

        if (!reply?.ok) {
          return reject(
            new Error(
              `Subscription rejected (${reply?.status ?? "unknown"}): ` +
              (reply?.error || "No error details returned.")
            )
          );
        }

        resolve();
      }
    );
  });

  await waitFor(() => latest !== null, "initial snapshot");
}

async function main() {
  assert(email && password, "Set SIM_TEST_EMAIL and SIM_TEST_PASSWORD.");

  beginStage("login");

  const login = await request("/auth/login", "POST", { email, password });
  token = login.access_token;

  assert(
    typeof token === "string" && token.length > 0,
    "Login response did not contain an access token."
  );

  beginStage("create temporary task and VM");

  const createdTask = await request("/tasks", "POST", {
    name: "WebSocket integration test task",
    work_mi: 2000,
    arrival_time: 0,
  });
  taskId = createdTask.task.id;

  const createdVM = await request("/vms", "POST", {
    name: "WebSocket integration test VM",
    capacity_mips: 100,
    overload_threshold: 10,
  });
  vmId = createdVM.vm.id;

  beginStage("create simulation");

  const createdRun = await request("/simulations", "POST", {
    task_ids: [taskId],
    vm_ids: [vmId],
    algorithm: "LPT",
    seed: 42,
    enable_redistribution: true,
  });

  const id = createdRun.simulation.id;
  simulationIds.push(id);

  socket = io(BASE, {
    autoConnect: false,
    forceNew: true,
    transports: ["websocket"],
    reconnection: false,
    auth: { token },
    extraHeaders: {
      Origin: process.env.SIM_TEST_ORIGIN || "http://localhost:5173",
    },
  });

  socket.on("simulation_state", (state) => {
    if (
      state.id === selectedId &&
      (!latest || state.revision > latest.revision)
    ) {
      latest = state;
    }
  });

  socket.on("auth_error", (data) => {
    socketError = new Error(data?.error || "Authentication failed.");
  });

  socket.on("disconnect", (reason) => {
    console.log(`Socket disconnected: ${reason}`);
  });

  beginStage("connect WebSocket");
  await connectSocket();

  beginStage("subscribe and receive initial snapshot");
  await subscribe(id);
  assert.equal(latest.status, "CREATED");

  beginStage("start simulation and receive automatic progress");
  await request(`/simulations/${id}/start`, "POST");

  await waitFor(
    () => latest?.status === "RUNNING" && latest.simulated_time >= 0.5,
    "automatic progress"
  );
  console.log("PASS: live updates and automatic execution");

  beginStage("pause simulation");

  const paused = await request(`/simulations/${id}/pause`, "POST");

  await waitFor(() => latest?.status === "PAUSED", "pause update");
  await delay(700);

  const pausedCheck = await request(`/simulations/${id}`);
  assert.equal(pausedCheck.simulation.status, "PAUSED");
  assert.equal(
    pausedCheck.simulation.simulated_time,
    paused.simulation.simulated_time
  );
  console.log("PASS: pause freezes simulated time");

  beginStage("resume while disconnected");

  socket.disconnect();

  await request(`/simulations/${id}/resume`, "POST");
  await delay(1000);

  const offlineProgress = await request(`/simulations/${id}`);
  assert(
    offlineProgress.simulation.simulated_time >
      paused.simulation.simulated_time,
    "Simulation did not advance while the client was disconnected."
  );

  beginStage("reconnect and receive current snapshot");

  await connectSocket();
  await subscribe(id);

  assert(latest.revision >= offlineProgress.simulation.revision);
  console.log("PASS: disconnected execution and reconnect snapshot");

  beginStage("wait for simulation completion");

  await waitFor(
    () => latest?.status === "COMPLETED",
    "simulation completion",
    45000
  );

  assert.equal(latest.completed_tasks, 1);
  assert.equal(latest.simulated_time, 20);
  assert.equal(latest.tasks[0].status, "COMPLETED");
  console.log("PASS: completion arrives over WebSocket");

  beginStage("reset into a new simulation");

  const reset = await request(`/simulations/${id}/reset`, "POST");
  const freshId = reset.simulation.id;
  simulationIds.push(freshId);

  assert.notEqual(freshId, id);
  assert.equal(reset.simulation.status, "CREATED");

  await subscribe(freshId);

  beginStage("start and stop the new simulation");

  await request(`/simulations/${freshId}/start`, "POST");

  const stopped = await request(`/simulations/${freshId}/stop`, "POST");
  await waitFor(() => latest?.status === "STOPPED", "stop update");

  await delay(700);

  const stoppedCheck = await request(`/simulations/${freshId}`);
  assert.equal(stoppedCheck.simulation.status, "STOPPED");
  assert.equal(
    stoppedCheck.simulation.simulated_time,
    stopped.simulation.simulated_time
  );

  const original = await request(`/simulations/${id}`);
  assert.equal(original.simulation.status, "COMPLETED");

  console.log("PASS: stop and reset preserve the original run");
  console.log(`Completed simulation: ${id}`);
}

async function cleanup() {
  socket?.disconnect();

  if (!token) return;

  for (const id of simulationIds) {
    try {
      const response = await request(`/simulations/${id}`);

      if (
        ["CREATED", "RUNNING", "PAUSED"].includes(
          response.simulation.status
        )
      ) {
        await request(`/simulations/${id}/stop`, "POST");
      }
    } catch (error) {
      console.error(`Run cleanup warning: ${error.message}`);
      process.exitCode = 1;
    }
  }

  for (const [collection, id] of [
    ["tasks", taskId],
    ["vms", vmId],
  ]) {
    if (!id) continue;

    try {
      await request(`/${collection}/${id}`, "DELETE");
    } catch (error) {
      console.error(`Input cleanup warning: ${error.message}`);
      process.exitCode = 1;
    }
  }
}

try {
  await main();
} catch (error) {
  console.error(`FAIL during "${stage}": ${error.message}`);
  process.exitCode = 1;
} finally {
  await cleanup();
}

if (!process.exitCode) {
  console.log("All real-time integration checks passed.");
  console.log("Temporary task and VM removed.");
}