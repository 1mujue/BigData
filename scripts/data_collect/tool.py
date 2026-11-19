import json
import hashlib
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def file_sha256(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=path.parent) as directory:
        temporary = Path(directory) / path.name
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)


def write_jsonl(path, records):
    """Write UTF-8 JSONL and replace the destination after all rows are written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=path.parent) as temp_dir:
        temp_path = Path(temp_dir) / path.name
        with temp_path.open("w", encoding="utf-8") as output:
            for record in records:
                print(json.dumps(record, ensure_ascii=False), file=output)
        temp_path.replace(path)


@contextmanager
def spark_session(app_name):
    """Run a local Spark session configured for the Compose HDFS endpoints."""
    from pyspark.sql import SparkSession

    spark = (
        SparkSession.builder
        .master("local[2]")
        .appName(app_name)
        .config("spark.hadoop.dfs.client.use.datanode.hostname", "true")
        .getOrCreate()
    )
    try:
        yield spark
    finally:
        spark.stop()
