from datetime import datetime, timezone

from bson import ObjectId
from flask import current_app
from pymongo import ReturnDocument


def get_simulations_collection():
    return current_app.extensions["mongo_db"]["simulations"]


def create_simulation_indexes():
    get_simulations_collection().create_index(
        [("owner_id", 1), ("created_at", -1), ("_id", -1)]
    )


def load_owned_inputs(owner_id, task_ids, vm_ids):
    db = current_app.extensions["mongo_db"]
    owner = ObjectId(owner_id)

    tasks = list(db["tasks"].find({
        "owner_id": owner,
        "_id": {"$in": [ObjectId(value) for value in task_ids]},
    }))
    vms = list(db["virtual_machines"].find({
        "owner_id": owner,
        "_id": {"$in": [ObjectId(value) for value in vm_ids]},
    }))

    task_map = {str(task["_id"]): task for task in tasks}
    vm_map = {str(vm["_id"]): vm for vm in vms}

    if len(task_map) != len(task_ids) or len(vm_map) != len(vm_ids):
        return None

    return (
        [task_map[value] for value in task_ids],
        [vm_map[value] for value in vm_ids],
    )


def insert_simulation(owner_id, document):
    document = dict(document)
    document["owner_id"] = ObjectId(owner_id)
    document["created_at"] = datetime.now(timezone.utc)
    document["revision"] = 0

    result = get_simulations_collection().insert_one(document)
    document["_id"] = result.inserted_id
    return document


def find_simulation(owner_id, simulation_id):
    if (
        not isinstance(simulation_id, str)
        or not ObjectId.is_valid(simulation_id)
    ):
        return None

    return get_simulations_collection().find_one({
        "_id": ObjectId(simulation_id),
        "owner_id": ObjectId(owner_id),
    })


def list_simulations(owner_id, page, limit):
    query = {"owner_id": ObjectId(owner_id)}
    collection = get_simulations_collection()

    documents = list(
        collection.find(
            query,
            {
                "created_at": 1,
                "revision": 1,
                "snapshot.status": 1,
                "snapshot.algorithm": 1,
                "snapshot.simulated_time": 1,
                "snapshot.completed_tasks": 1,
                "snapshot.total_tasks": 1,
            },
        )
        .sort([("created_at", -1), ("_id", -1)])
        .skip((page - 1) * limit)
        .limit(limit)
    )

    return documents, collection.count_documents(query)


def save_simulation_command(owner_id, simulation_id, revision, state, command):
    return get_simulations_collection().find_one_and_update(
        {
            "_id": ObjectId(simulation_id),
            "owner_id": ObjectId(owner_id),
            "revision": revision,
        },
        {
            "$set": {
                "snapshot": state,
                "updated_at": datetime.now(timezone.utc),
            },
            "$push": {"commands": command},
            "$inc": {"revision": 1},
        },
        return_document=ReturnDocument.AFTER,
    )
def list_active_simulation_ids():
    documents = get_simulations_collection().find(
        {"snapshot.status": {"$in": ["RUNNING", "PAUSED"]}},
        {"_id": 1, "owner_id": 1},
    )

    return [
        (str(document["owner_id"]), str(document["_id"]))
        for document in documents
    ]