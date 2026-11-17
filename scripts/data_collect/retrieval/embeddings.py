"""Content-addressed embedding cache and externally generated query vectors."""
import hashlib
import json
import sys
import os
import sqlite3
from pathlib import Path

import numpy as np

from ..network import HttpClient


def document_text(row):
    value = "Title: " + row["title"].strip()
    if row.get("abstract"):
        value += "\nAbstract: " + row["abstract"].strip()
    return value


def query_text(text, config):
    instruction = config.get("query_instruction", "Retrieve research papers relevant to the described research task.")
    return f"Instruct: {instruction}\nQuery: {text}"


def vector_signature(config):
    material = {key: config[key] for key in ("endpoint", "model", "dimensions")}
    material["document_format"] = "title-abstract-v1"
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def validate_vectors(values, count, dimensions):
    matrix = np.asarray(values, dtype=np.float32)
    if matrix.shape != (count, dimensions) or not np.isfinite(matrix).all():
        raise ValueError(f"Invalid embedding shape/data: {matrix.shape}; expected {(count, dimensions)}")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise ValueError("Embedding contains a zero vector")
    return matrix / norms


class EmbeddingAPI:
    def __init__(self, config, client=None):
        self.config = config
        self.client = client or HttpClient()

    def encode(self, texts):
        key = next((os.environ[name] for name in ("EMBEDDING_API_KEY", "SILICONFLOW_API_KEY", "Silicon_API_KEY")
                    if os.environ.get(name)), None)
        if not key:
            raise ValueError("Missing embeddings require EMBEDDING_API_KEY (or SILICONFLOW_API_KEY)")
        payload = self.client.json(self.config["endpoint"],
            data=json.dumps({"model": self.config["model"], "dimensions": self.config["dimensions"],
                             "input": texts, "encoding_format": "float"}),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
        data = sorted(payload["data"], key=lambda row: row["index"])
        if [row["index"] for row in data] != list(range(len(texts))):
            raise ValueError("Embedding response does not cover the requested inputs")
        return validate_vectors([row["embedding"] for row in data], len(texts), self.config["dimensions"]), payload.get("usage", {}).get("total_tokens", 0)


class EmbeddingCache:
    def __init__(self, path, config):
        self.config = config
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS embeddings (key TEXT PRIMARY KEY, vector BLOB NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS usage (created_at TEXT DEFAULT CURRENT_TIMESTAMP, model TEXT, input_count INTEGER, tokens INTEGER)")
        self.new_vectors = 0
        self.new_tokens = 0

    def key(self, text):
        c = self.config
        return hashlib.sha256(f'{c["endpoint"]}\n{c["model"]}\n{c["dimensions"]}\n{text}'.encode()).hexdigest()

    def get_or_create(self, texts, request, batch_size=32):
        keys = [self.key(text) for text in texts]
        unique = dict(zip(keys, texts))
        vectors = {}
        key_list = list(unique)
        for start in range(0, len(key_list), 400):
            batch = key_list[start:start + 400]
            placeholders = ",".join("?" for _ in batch)
            for key, blob in self.db.execute(f"SELECT key, vector FROM embeddings WHERE key IN ({placeholders})", batch):
                vector = np.frombuffer(blob, dtype=np.float32)
                if vector.shape != (self.config["dimensions"],) or not np.isfinite(vector).all() or not np.any(vector):
                    raise ValueError("Invalid cached embedding")
                vectors[key] = vector
        missing = [key for key in key_list if key not in vectors]
        print(f"Embeddings: {len(unique)} inputs, {len(vectors)} cached, {len(missing)} missing", file=sys.stderr, flush=True)
        for start in range(0, len(missing), batch_size):
            batch = missing[start:start + batch_size]
            values, tokens = request([unique[key] for key in batch])
            values = validate_vectors(values, len(batch), self.config["dimensions"])
            with self.db:
                self.db.executemany("INSERT INTO embeddings (key,vector) VALUES (?,?)", [(key, value.tobytes()) for key, value in zip(batch, values)])
                self.db.execute("INSERT INTO usage (model,input_count,tokens) VALUES (?,?,?)", (self.config["model"], len(batch), tokens))
            vectors.update(zip(batch, values))
            self.new_vectors += len(batch)
            self.new_tokens += tokens
        return np.stack([vectors[key] for key in keys]) if keys else np.empty((0, self.config["dimensions"]), dtype=np.float32)

    def close(self):
        self.db.close()
