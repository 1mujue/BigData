"""Run Spark SQL over a complete, validated reading run."""
from pathlib import Path

from .screening import analysis_row, complete_records, new_directory, require, write_json, write_rows

QUERIES = {
    "by_tier": "SELECT stage, tier, COUNT(*) AS papers FROM papers GROUP BY stage, tier ORDER BY stage, tier",
    "by_source_year": "SELECT venue, year, tier, COUNT(*) AS papers FROM papers GROUP BY venue, year, tier ORDER BY venue, year, tier",
    "by_implementation": "SELECT implementation_status, tier, COUNT(*) AS papers FROM papers WHERE stage = 'detail' GROUP BY implementation_status, tier ORDER BY implementation_status, tier",
    "needs_evidence": "SELECT paper_id, title, status_reasons, next_checks FROM papers WHERE tier = 'needs_evidence' ORDER BY paper_id",
    "paper_datasets": "SELECT dataset, COUNT(DISTINCT paper_id) AS papers FROM papers LATERAL VIEW explode(datasets) d AS dataset WHERE stage = 'detail' GROUP BY dataset ORDER BY papers DESC, dataset",
    "paper_models": "SELECT original_model, COUNT(DISTINCT paper_id) AS papers FROM papers LATERAL VIEW explode(models) m AS original_model WHERE stage = 'detail' GROUP BY original_model ORDER BY papers DESC, original_model",
    "eligible_ranked": "SELECT * FROM papers WHERE rankable = true ORDER BY CASE tier WHEN 'priority' THEN 0 ELSE 1 END, score DESC, paper_id",
}


def schema():
    from pyspark.sql import types as T
    string_fields = ("paper_id", "title", "venue", "tier", "rubric_version", "batch_id", "stage", "reading_scope", "model", "training", "implementation_status")
    fields = [T.StructField(k, T.StringType(), True) for k in string_fields]
    fields += [T.StructField(k, T.IntegerType(), True) for k in ("year", "A", "B", "C", "D")]
    fields.append(T.StructField("score", T.DoubleType(), True))
    fields += [T.StructField(k, T.BooleanType(), True) for k in ("rankable", "source_eligible", "benchmark_primary", "in_scope")]
    fields += [T.StructField(k, T.ArrayType(T.StringType(), False), True) for k in ("topics", "contribution_types", "models", "datasets", "metrics", "next_checks", "status_reasons")]
    return T.StructType(fields)


def analyze(run, output, master="local[2]"):
    records, checked = complete_records(run)
    require(not Path(output).exists(), "output path already exists; choose a new directory")
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.master(master).appName("paper-fine-screening").getOrCreate()
    try:
        frame = spark.createDataFrame([analysis_row(row) for row in records], schema=schema()).cache()
        frame.createOrReplaceTempView("papers")
        out = new_directory(output)
        frame.write.mode("errorifexists").parquet(str(out / "papers.parquet"))
        for name, sql in QUERIES.items():
            result = spark.sql(sql)
            if name == "eligible_ranked":
                result.write.mode("errorifexists").parquet(str(out / "eligible_ranked.parquet"))
            write_rows(out / (name + ".jsonl"), [r.asDict(recursive=True) for r in result.collect()])
        (out / "queries.sql").write_text("\n\n".join("-- " + name + "\n" + query + ";" for name, query in QUERIES.items()) + "\n", encoding="utf-8")
        summary = {"stage": checked["stage"], "population": "complete supplied reading run", "papers": checked["valid"],
                   "tiers": checked["tiers"], "spark_version": spark.version,
                   "rubric_version": checked["rubric_version"], "batch_id": checked["batch_id"],
                   "run_manifest_sha256": checked["run_manifest_sha256"]}
        write_json(out / "summary.json", summary)
        return summary
    finally:
        spark.stop()
