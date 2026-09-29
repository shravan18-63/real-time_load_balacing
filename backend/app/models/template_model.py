from flask import current_app


def get_templates_collection():
    return current_app.extensions["mongo_db"]["simulation_templates"]


def create_template_indexes():
    get_templates_collection().create_index(
        [("owner_id", 1), ("created_at", -1), ("_id", -1)]
    )
