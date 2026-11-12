import argparse

from pyspark.sql import SparkSession

parser = argparse.ArgumentParser(description="校验 HDFS 中的论文元数据")
parser.add_argument("hdfs_uri", help="输入 JSONL 的 HDFS URI")
parser.add_argument("--expected-count", required=True, type=int)
args = parser.parse_args()

spark = (
    SparkSession.builder
    .master("local[2]")
    .appName("validate-paper-metadata")
    .config("spark.hadoop.dfs.client.use.datanode.hostname","true")
    .getOrCreate()
)

try:
    papers = (
        spark.read
        .schema("dblp_id STRING, title STRING, year INT, venue_stream STRING, doi_url STRING")
        .option("mode","FAILFAST")
        .json(args.hdfs_uri)
    )
    stats = papers.selectExpr(
        "count(*) AS total_rows",
        "count(DISTINCT dblp_id) AS unique_ids",
        """
        count_if(
            coalesce(trim(dblp_id), '') = ''
            OR coalesce(trim(title), '') = ''
            OR year IS NULL
            OR coalesce(trim(venue_stream), '') = ''
        ) AS invalid_rows
        """,
        "sort_array(collect_set(venue_stream)) AS venues",
        "sort_array(collect_set(year)) AS years",
    ).first()
finally:
    spark.stop()

print(stats.asDict())
if stats.total_rows != args.expected_count:
    raise ValueError(f"总行数 {stats.total_rows} 不等于预期 {args.expected_count}")
if stats.unique_ids != stats.total_rows:
    raise ValueError(f"发现 {stats.total_rows - stats.unique_ids} 行重复的论文 ID")
if stats.invalid_rows > 0:
    raise ValueError(f"存在 {stats.invalid_rows} 行无效数据")
print("校验通过")