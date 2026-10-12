
import os
import pathlib

from bson import json_util
from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()

here = pathlib.Path(__file__).parent / "sample_data"
opts = json_util.JSONOptions(tz_aware=True)

client = MongoClient(
    os.getenv("MONGO_URI"),
    tz_aware=True,
    serverSelectionTimeoutMS=10000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

try:
    for name in ("employees", "attendance_logs"):
        file_path = here / f"{name}.json"
        docs = json_util.loads(
            file_path.read_text(encoding="utf-8"),
            json_options=opts,
        )
        db[name].delete_many({})
        if docs:
            db[name].insert_many(docs)
        print(f"Loaded {len(docs)} documents into {db.name}.{name}")
finally:
    client.close()
