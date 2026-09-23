from flask import Flask
from pymongo import MongoClient

from config import Config
from extensions import cors, jwt, socketio

from .models.user_model import create_user_indexes
from .models.task_model import create_task_indexes
from .models.vm_model import create_vm_indexes
from .models.simulation_model import create_simulation_indexes

from .routes.auth_routes import auth_bp
from .routes.task_routes import task_bp
from .routes.vm_routes import vm_bp
from .routes.simulation_routes import simulation_bp

from .sockets.simulation_socket import SimulationNamespace
from .services.simulation_runner import SimulationRunner


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    secret = app.config.get("JWT_SECRET_KEY")
    if not secret or len(secret) < 32:
        raise RuntimeError(
            "Set JWT_SECRET_KEY to a random secret of at least 32 characters."
        )

    app.config["SECRET_KEY"] = secret
    jwt.init_app(app)

    socketio.init_app(
        app,
        cors_allowed_origins=[app.config["FRONTEND_ORIGIN"]],
        max_http_buffer_size=100_000,
    )

    cors.init_app(
        app,
        resources={
            r"/api/*": {
                "origins": [app.config["FRONTEND_ORIGIN"]]
            }
        },
    )

    mongo_client = MongoClient(
        app.config["MONGO_URI"],
        serverSelectionTimeoutMS=5000,
        tz_aware=True,
    )

    try:
        mongo_client.admin.command("ping")
    except Exception:
        mongo_client.close()
        raise

    app.extensions["mongo_client"] = mongo_client
    app.extensions["mongo_db"] = mongo_client[
        app.config["MONGO_DB_NAME"]
    ]

    app.logger.setLevel("INFO")
    app.logger.info("MongoDB connection verified.")

    with app.app_context():
        create_user_indexes()
        create_task_indexes()
        create_vm_indexes()
        create_simulation_indexes()

    app.logger.info("User, task, VM, and simulation indexes verified.")

    app.register_blueprint(auth_bp)
    app.register_blueprint(task_bp)
    app.register_blueprint(vm_bp)
    app.register_blueprint(simulation_bp)

    simulation_namespace = SimulationNamespace()
    socketio.on_namespace(simulation_namespace)
    app.extensions["simulation_namespace"] = simulation_namespace

    simulation_runner = SimulationRunner(app, socketio)
    app.extensions["simulation_runner"] = simulation_runner

    with app.app_context():
        simulation_runner.recover()

    return app