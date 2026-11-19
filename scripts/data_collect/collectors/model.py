"""Normalized records and identity-preserving merges."""
import html
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse


def now():
    return datetime.now(timezone.utc).isoformat()


def text(value):
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def title_key(value):
    return re.sub(r"[^\w]", "", text(value).casefold())


def forum_id(url):
    return parse_qs(urlparse(url or "").query).get("id", [None])[0] if "openreview.net" in (url or "") else None


def paper(venue, year, source_id, title, source):
    return {"id":f"{venue}:{year}:{source_id}", "venue":venue, "year":year,
            "title":text(title), "authors":[], "abstract":None, "tags":[],
            "doi":None, "openreview_id":None, "paper_url":None, "pdf_urls":[],
            "publication_date":None, "publication":{}, "scope":{},
            "source_urls":[source], "field_sources":{}, "errors":[],
            "collected_at":now(), "pdf_checks":{}}


def set_field(row, field, value, source):
    if value is not None and value != "" and value != []:
        row[field]=value
        row["field_sources"].setdefault(field, [])
        if source not in row["field_sources"][field]:
            row["field_sources"][field].append(source)


def add_tags(row, kind, values, source):
    if isinstance(values,str):
        values=[v.strip() for v in values.split(";") if v.strip()]
    for value in values or []:
        value=text(value)
        tag={"kind":kind,"value":value,"source":source}
        if value and tag not in row["tags"]:
            row["tags"].append(tag)


def add_pdf(row, url, source, derived=False):
    if url and urlparse(url).path.lower().endswith(('.html','.htm')):
        return
    if url and url.startswith(("http://","https://")) and url not in row["pdf_urls"]:
        row["pdf_urls"].append(url)
        row["field_sources"].setdefault("pdf_urls",[])
        if source not in row["field_sources"]["pdf_urls"]:
            row["field_sources"]["pdf_urls"].append(source)
        row["pdf_checks"][url]={"status":"not_checked", "link_origin":"canonical_endpoint" if derived else "source"}


def merge_into(a,b):
    for key in ("abstract","doi","openreview_id","paper_url","publication_date","authors"):
        if not a.get(key) and b.get(key):a[key]=b[key]
    for key in ("tags","source_urls","errors"):
        for value in b.get(key,[]):
            if value not in a[key]:a[key].append(value)
    for url in b.get("pdf_urls",[]):
        if url not in a["pdf_urls"]:a["pdf_urls"].append(url)
    for field,sources in b.get("field_sources",{}).items():
        for source in sources:
            if source not in a["field_sources"].setdefault(field,[]):a["field_sources"][field].append(source)
    for key in ("publication","scope","pdf_checks"):
        for k,v in b.get(key,{}).items():
            if not a[key].get(k):a[key][k]=v
    return a


def merge_papers(rows):
    output=[]; identities={}
    for row in rows:
        if not row["title"]:raise ValueError("A collected paper has no title")
        prefix=(row["venue"],row["year"])
        keys=[prefix+("id",row["id"])]
        for field in ("doi","openreview_id","paper_url"):
            if row.get(field):keys.append(prefix+(field,str(row[field]).lower() if field=='doi' else str(row[field])))
        found={identities[k] for k in keys if k in identities}
        if len(found)>1:raise ValueError(f"Conflicting identities for {row['id']}")
        if found:
            i=found.pop();merge_into(output[i],row)
        else:
            i=len(output);output.append(row)
        for k in keys:identities[k]=i
    return output


def finalize(row):
    row["availability"]={}
    for field in ("title","abstract","tags","pdf_urls"):
        if row.get(field):status="present"
        elif any(field in e.get("fields",[]) for e in row.get("errors",[])):status="request_failed"
        else:status="not_provided"
        row["availability"][field]=status
    row["missing_fields"]=[f for f,s in row["availability"].items() if s!="present"]
    return row
