from flask import Blueprint


template_bp = Blueprint(
    "templates",
    __name__,
    url_prefix="/api/templates",
)
