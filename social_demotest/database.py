from pymongo import MongoClient
from config import MONGO_URI

# ----------------- DB Initialization -----------------
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["profiling_db"]
profiles_coll = db["profiles"]
matches_coll = db["matches"]
messages_coll = db["messages"]
