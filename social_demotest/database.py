from pymongo import MongoClient
from config import MONGO_URI, MONGO_DB_NAME

# ----------------- DB Initialization -----------------
mongo_client = MongoClient(MONGO_URI)
db = mongo_client[MONGO_DB_NAME]
profiles_coll = db["profiles"]
matches_coll = db["matches"]
messages_coll = db["messages"]
