import json
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory


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
