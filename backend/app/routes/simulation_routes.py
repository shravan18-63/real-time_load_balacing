from functools import wraps

from flask import Blueprint, current_app, jsonify, request
from flask_jwt_extended import get_jwt_identity, jwt_required
from pymongo.errors import PyMongoError

from ..services.auth_service import AuthServiceError, get_current_user
from ..services.simulation_service import (
    SimulationServiceError,
    control_simulation_for_user,
    create_simulation_for_user,
    get_simulation_for_user,
    list_simulations_for_user,
)

simulation_bp = Blueprint(
    "simulations",
    __name__,
    url_prefix="/api/simulations",
)


def authenticated_simulation_route(view):
    @wraps(view)
    @jwt_required()
    def wrapped(*args, **kwargs):
        try:
            user = get_current_user(get_jwt_identity())
            return view(user["id"], *args, **kwargs)
        except (AuthServiceError, SimulationServiceError) as exc:
            return jsonify({"error": str(exc)}), exc.status_code
        except PyMongoError:
            current_app.logger.exception(
                "Simulation database operation failed."
            )
            return jsonify({
                "error": "Database operation failed. Please try again."
            }), 503

    return wrapped


def read_json_body():
    if not request.is_json:
        raise SimulationServiceError(
            "Content-Type must be application/json.",
            status_code=415,
        )

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise SimulationServiceError(
            "Request body must be a valid JSON object."
        )

    return data


@simulation_bp.post("")
@authenticated_simulation_route
def create(owner_id):
    simulation = create_simulation_for_user(
        owner_id, read_json_body()
    )
    return jsonify({"simulation": simulation}), 201


@simulation_bp.get("")
@authenticated_simulation_route
def list_runs(owner_id):
    result = list_simulations_for_user(
        owner_id,
        page=request.args.get("page", "1"),
        limit=request.args.get("limit", "20"),
    )
    return jsonify(result), 200


@simulation_bp.get("/<simulation_id>")
@authenticated_simulation_route
def get_run(owner_id, simulation_id):
    simulation = get_simulation_for_user(owner_id, simulation_id)
    return jsonify({"simulation": simulation}), 200


@simulation_bp.post("/<simulation_id>/<action>")
@authenticated_simulation_route

def control(owner_id, simulation_id, action):
    if action == "advance":
        raise SimulationServiceError(
            "Simulation time now advances automatically.",
            status_code=409,
        )

    simulation = control_simulation_for_user(
        owner_id,
        simulation_id,
        action,
    )

    try:
        current_app.extensions["simulation_namespace"].publish(
            owner_id,
            simulation,
        )
    except Exception:
        current_app.logger.exception(
            "Simulation saved, but socket delivery failed."
        )

    if simulation["status"] == "RUNNING":
        current_app.extensions["simulation_runner"].ensure_running(
            owner_id,
            simulation["id"],
        )

    return jsonify({"simulation": simulation}), (
        201 if action == "reset" else 200
    )