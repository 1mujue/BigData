import argparse

from pyspark.sql import SparkSession, Window
from pyspark.sql import functions as F

parser = argparse.ArgumentParser(description="生成去重后的论文元数据表")
parser.add_argument("input_uri", help="已校验的 JSONL 批次目录")
parser.add_argument("output_uri", help="本次生成的 Parquet 目录")
args = parser.parse_args()

spark = (
    SparkSession.builder
    .master("local[2]")
    .appName("build-paper-metadata-table")
    .config("spark.hadoop.dfs.client.use.datanode.hostname", "true")
    .getOrCreate()
)

try:
    raw = (
        spark.read
        .schema("dblp_id STRING, title STRING, year INT, venue_stream STRING, doi_url STRING")
        .option("mode", "FAILFAST")
        .option("recursiveFileLookup", "true")
        .option("pathGlobFilter", "*.jsonl")
        .json(args.input_uri)
        .withColumn("doi_url", F.expr("nullif(trim(doi_url), '')"))
    )

    preference = Window.partitionBy("dblp_id").orderBy(
        F.col("doi_url").isNotNull().desc(),
        F.col("title").asc(),
        F.col("year").desc(),
        F.col("venue_stream").asc(),
        F.col("doi_url").asc_nulls_last(),
    )
    curated = (
        raw.withColumn("row_number", F.row_number().over(preference))
        .where(F.col("row_number") == 1)
        .drop("row_number")
        .withColumn("paper_id", F.sha2("dblp_id", 256))
    )

    print(f"输入 {raw.count()} 行")
    curated.write.mode("errorifexists").parquet(args.output_uri)

    saved = spark.read.parquet(args.output_uri)
    saved.selectExpr(
        "count(*) AS saved_rows",
        "count(DISTINCT paper_id) AS unique_paper_ids",
    ).show()
finally:
    spark.stop()