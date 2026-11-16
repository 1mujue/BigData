import json
import re

from .network import HttpClient


class DblpCollector:
    """Collect DBLP records from one venue over an inclusive publication year range."""

    def __init__(self, stream, year, doi=None, *, end_year=None, issue=None, client=None):
        if not re.fullmatch(r"(?:conf|journals)/[A-Za-z0-9][A-Za-z0-9_.-]*", stream):
            raise ValueError("--stream 应为 conf/简称 或 journals/简称")

        self.stream = stream
        self.start_year = year
        self.end_year = year if end_year is None else end_year
        self.doi = doi
        self.issue = issue
        self.client = client or HttpClient()

        if not 1000 <= self.start_year <= self.end_year <= 9999:
            raise ValueError("年份须为四位整数，且起始年份不能晚于结束年份")

    def collect(self, limit: int | None, page_size: int):
        if limit is not None and limit < 1:
            raise ValueError("--limit 必须是正整数")
        if page_size < 1:
            raise ValueError("--page-size 必须是正整数")

        records = {}
        offset = 0
        while limit is None or len(records) < limit:
            page_limit = page_size if limit is None else min(page_size, limit - len(records))
            rows = self._fetch_page(page_limit, offset)
            for row in rows:
                dblp_id = row["paper"]["value"]
                record = records.setdefault(dblp_id, {
                    "dblp_id": dblp_id,
                    "title": row["title"]["value"],
                    "year": int(row["year"]["value"]),
                    "venue_stream": self.stream,
                    "doi_url": row.get("doi", {}).get("value"),
                    "authors": [], "document_urls": [], "toc_urls": [],
                    "publication_type": row.get("kind", {}).get("value", "").rsplit("#", 1)[-1],
                })
                for source, field in (("author", "authors"), ("link", "document_urls"), ("toc", "toc_urls")):
                    value = row.get(source, {}).get("value")
                    if value and value not in record[field]:
                        record[field].append(value)

            print(f"偏移量 {offset}，返回 {len(rows)} 行，累计 {len(records)} 篇")
            returned_papers = len({row["paper"]["value"] for row in rows})
            offset += returned_papers
            if returned_papers < page_limit:
                break
        return list(records.values())

    def _fetch_page(self, page_limit, offset):
        doi_filter = ""
        if self.doi:
            doi = json.dumps(self.doi.strip().lower())
            doi_filter = (
                'FILTER(REPLACE(LCASE(STR(?doi)), "^https?://(dx[.])?doi[.]org/", "") = '
                f"{doi})"
            )
        year_filter = f'STR(?year) = "{self.start_year}"'
        if self.start_year != self.end_year:
            year_filter = (
                f'STR(?year) >= "{self.start_year}" && STR(?year) <= "{self.end_year}"'
            )

        issue_filter = f"?paper dblp:publishedInJournalVolumeIssue {json.dumps(self.issue)} ." if self.issue else ""
        query = f"""
PREFIX dblp: <https://dblp.org/rdf/schema#>

SELECT DISTINCT ?paper ?title ?year ?doi ?author ?link ?toc ?kind WHERE {{
 {{ SELECT DISTINCT ?paper WHERE {{
  ?paper dblp:publishedInStream <https://dblp.org/streams/{self.stream}> ;
         dblp:yearOfPublication ?year ; a ?kind .
  VALUES ?kind {{ dblp:Inproceedings dblp:Article }}
  FILTER({year_filter})
  OPTIONAL {{ ?paper dblp:doi ?doi . }}
  {doi_filter}
  {issue_filter}
 }} ORDER BY ?paper LIMIT {page_limit} OFFSET {offset} }}
 ?paper dblp:title ?title ; dblp:yearOfPublication ?year ; a ?kind .
 VALUES ?kind {{ dblp:Inproceedings dblp:Article }}
 OPTIONAL {{ ?paper dblp:doi ?doi . }}
 OPTIONAL {{ ?paper dblp:authoredBy/dblp:primaryCreatorName ?author . }}
 OPTIONAL {{ ?paper dblp:documentPage ?link . }}
 OPTIONAL {{ ?paper dblp:listedOnTocPage ?toc . }}
}}
ORDER BY ?paper ?author ?link ?toc
"""
        return self.client.json(
            "https://sparql.dblp.org/sparql",
            data=query,
            headers={
                "Content-Type": "application/sparql-query",
                "Accept": "application/sparql-results+json",
            },
        )["results"]["bindings"]


class MetadataTable:
    SCHEMA = ("dblp_id STRING, title STRING, year INT, venue_stream STRING, doi_url STRING, "
              "authors ARRAY<STRING>, document_urls ARRAY<STRING>, toc_urls ARRAY<STRING>, "
              "publication_type STRING, venue STRING")

    def __init__(self, spark):
        self.spark = spark

    def validate(self, input_uri, expected_count):
        stats = self._read_jsonl(input_uri).selectExpr(
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

        print(stats.asDict())
        if stats.total_rows != expected_count:
            raise ValueError(f"总行数 {stats.total_rows} 不等于预期 {expected_count}")
        if stats.unique_ids != stats.total_rows:
            raise ValueError(f"发现 {stats.total_rows - stats.unique_ids} 行缺失或重复的论文 ID")
        if stats.invalid_rows:
            raise ValueError(f"存在 {stats.invalid_rows} 行无效数据")
        print("校验通过")

    def build(self, input_uri, output_uri, *, mode="errorifexists"):
        from pyspark.sql import Window
        from pyspark.sql import functions as F

        raw = self._read_jsonl(input_uri, recursive=True).withColumn(
            "doi_url", F.expr("nullif(trim(doi_url), '')")
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
        curated.write.mode(mode).parquet(output_uri)
        self.spark.read.parquet(output_uri).selectExpr(
            "count(*) AS saved_rows",
            "count(DISTINCT paper_id) AS unique_paper_ids",
        ).show()

    def _read_jsonl(self, uri, *, recursive=False):
        reader = self.spark.read.schema(self.SCHEMA).option("mode", "FAILFAST")
        if recursive:
            reader = reader.option("recursiveFileLookup", "true").option(
                "pathGlobFilter", "*.jsonl"
            )
        return reader.json(uri)
