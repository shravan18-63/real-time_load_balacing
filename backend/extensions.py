from flask_cors import CORS
from flask_jwt_extended import JWTManager
from flask_socketio import SocketIO

cors = CORS()
jwt = JWTManager()

socketio = SocketIO(
    async_mode="threading",
    logger=False,
    engineio_logger=False,
)