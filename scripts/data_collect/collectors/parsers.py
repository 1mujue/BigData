"""Parse publisher metadata, without using paper bodies."""
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from .model import paper, text, set_field, add_tags, add_pdf, forum_id, title_key


def strip_html(value):
    return text(BeautifulSoup(value or "", "html.parser").get_text(" "))


def parse_acl(xml,year,source,venue,anthology,volumes):
    root=ET.fromstring(xml); rows=[]
    for volume in root.findall("volume"):
        if volume.get("id") not in volumes:continue
        for p in volume.findall("paper"):
            if p.get("id")=="0":continue
            pid=f"{anthology}-{volume.get('id')}.{p.get('id')}"
            title=p.find("title")
            row=paper(venue,year,pid,"".join(title.itertext()) if title is not None else "",source)
            row["paper_url"]=f"https://aclanthology.org/{pid}/"
            row["authors"]=[text(" ".join(filter(None,[a.findtext('first'),a.findtext('last')]))) for a in p.findall("author")]
            abstract=p.find("abstract")
            if abstract is not None:set_field(row,"abstract",text("".join(abstract.itertext())),source)
            row["doi"]=p.findtext("doi")
            if p.find("pdf") is not None:add_pdf(row,f"https://aclanthology.org/{pid}.pdf",source)
            row["scope"]={"status":"published","track":"main","type":volume.get("id")}
            row["publication"]={"anthology_id":pid,"volume":volume.get("id")}
            rows.append(row)
    return rows


def parse_miniconf(data,abstracts,venue,year,source,settings):
    events=data["results"] if isinstance(data,dict) else data
    by_id={str(e['id']):e for e in events}
    posters={};orals={}
    for e in events:
        sid=e.get('sourceid')
        if e.get('eventtype')=='Poster' and isinstance(sid,int) and sid>0:
            posters[sid]=e
        if e.get('eventtype')=='Oral' and isinstance(sid,int) and sid<0:
            orals[-sid]=e
    rows=[];excluded=0
    for event in events:
        origin=str(event.get("sourceurl") or "")
        if settings.get('source_contains') and settings['source_contains'].format(venue=venue,year=year) not in origin:
            excluded+=1;continue
        if any(x in origin.lower() for x in settings.get("excluded_origins",[])):
            excluded+=1;continue
        if event.get("eventtype",event.get("event_type","")) not in settings["event_types"]:
            excluded+=1;continue
        related=[by_id[str(i)] for i in event.get('related_events_ids',[]) if str(i) in by_id]
        sid=event.get('sourceid')
        if event.get('eventtype')=='Oral' and isinstance(sid,int) and sid<0 and -sid in posters:
            continue
        if event.get('eventtype')=='Poster' and sid in orals:related.append(orals[sid])
        if event.get('eventtype')=='Oral' and any(r.get('eventtype')=='Poster' and title_key(r.get('name'))==title_key(event.get('name')) for r in related):
            continue
        url=event.get("paper_url") or ""
        pdf=event.get("paper_pdf_url") or ""
        if pdf and urlparse(pdf).path.lower().endswith(('.html','.htm')):
            if not url:url=pdf
            pdf=""
        for medium in event.get("eventmedia",[]) or []:
            link=medium.get("uri") or ""
            if "openreview.net/forum" in link and not url:url=link
        rid=forum_id(url)
        sid=rid or url or str(event["id"])
        row=paper(venue,year,sid,event.get("name") or event.get("title"),source)
        row["paper_url"]=url or urljoin(source,event.get("virtualsite_url") or "")
        row["openreview_id"]=rid
        row["authors"]=[a.get("fullname","") if isinstance(a,dict) else a for a in (event.get("authors") or [])]
        abstract=event.get("abstract") or abstracts.get(str(event["id"]))
        if isinstance(abstract,dict):abstract=abstract.get("abstract")
        set_field(row,"abstract",strip_html(abstract),source)
        add_tags(row,"topic",event.get("topic"),source)
        add_tags(row,"keyword",event.get("keywords"),source)
        for related_event in related:
            if title_key(related_event.get('name'))==title_key(event.get('name')) or (isinstance(event.get('sourceid'),int) and related_event.get('sourceid')==-event['sourceid']):
                add_tags(row,'topic',related_event.get('topic'),source)
                add_tags(row,'keyword',related_event.get('keywords'),source)
        if pdf:add_pdf(row,urljoin(source,pdf),source)
        if rid:add_pdf(row,f"https://openreview.net/pdf?id={rid}",source,derived=True)
        row["scope"]={"status":"accepted","track":"position" if "position" in origin.lower() else ("main" if "/Conference" in origin else "unspecified"),
                      "type":"conference_paper","source_venue":origin,"decision":event.get("decision")}
        rows.append(row)
    return rows,excluded


def parse_aaai_page(xml,years,source,sections,prefixes):
    ns={"o":"http://www.openarchives.org/OAI/2.0/","dc":"http://purl.org/dc/elements/1.1/"}
    root=ET.fromstring(xml);err=root.find("o:error",ns)
    if err is not None:
        if err.get("code")=="noRecordsMatch":return [],None,0
        raise ValueError(f"OAI error {err.get('code')}: {err.text}")
    rows=[];ignored=0
    for rec in root.findall(".//o:record",ns):
        def vals(k):return [text("".join(x.itertext())) for x in rec.findall(f".//dc:{k}",ns)]
        dates=vals("date")
        sources=vals("source");citation=next((x for x in sources if "Vol." in x),"")
        edition=re.search(r'AAAI[-–](\d{2})\b',citation)
        year=2000+int(edition.group(1)) if edition else (int(dates[0][:4]) if dates else None)
        set_id=rec.findtext('o:header/o:setSpec',None,ns)
        section=(sections or {}).get(set_id,'')
        title=next(iter(vals("title")),"")
        technical=section.startswith(tuple(prefixes))
        if year not in years or not technical or title.upper().startswith("RETRACTED:"):
            ignored+=1;continue
        urls=vals("identifier");landing=next((x for x in urls if x.startswith("http")),"")
        doi=next((x for x in urls if x.startswith("10.")),None)
        row=paper("AAAI",year,landing.rsplit("/",1)[-1],title,source)
        row["paper_url"]=landing;row["doi"]=doi;row["authors"]=vals("creator")
        row["publication_date"]=dates[0];row["publication"]={"citation":citation}
        set_field(row,"abstract"," ".join(vals("description")),source)
        add_tags(row,"subject",vals("subject"),source)
        for url in vals("relation"):
            if "/article/view/" in url or "/article/download/" in url:add_pdf(row,url,source)
        row["scope"]={"status":"published","track":section or "technical","type":"conference_paper","section_id":set_id}
        rows.append(row)
    token=root.findtext(".//o:resumptionToken",None,ns)
    return rows,token or None,ignored


def researchr_abstract(description):
    """Remove the modal's author cards and publication buttons, not inline abstract links."""
    node=BeautifulSoup(str(description),'html.parser')
    for extra in node.select('.row, .publication-link, script, style'):
        extra.decompose()
    return node.get_text(' ',strip=True)


def publication_date(record):
    dates=[]
    for key in ("published-online","published-print","published","issued"):
        parts=record.get(key,{}).get("date-parts",[[]])[0]
        if parts:dates.append("-".join([str(parts[0])]+[f"{x:02}" for x in parts[1:]]))
    return min(dates) if dates else None


def meta_values(soup,key):
    return [text(x.get("content")) for x in soup.find_all("meta",attrs={"name":key}) if x.get("content")]


def parse_metadata_html(html,venue,year,url,source=None,page_metadata=None):
    soup=BeautifulSoup(html,"html.parser");source=source or url
    titles=meta_values(soup,"citation_title")
    title_node=soup.select_one("#papertitle, h1, h2")
    title=titles[0] if titles else (title_node.get_text(" ",strip=True) if title_node else "")
    row=paper(venue,year,url,title,source);row["paper_url"]=url
    row["authors"]=meta_values(soup,"citation_author")
    abstracts=meta_values(soup,"citation_abstract")+meta_values(soup,"DC.Description")
    abstract=soup.select_one("#abstract, .abstract, .abstractSection")
    if abstracts:set_field(row,"abstract",strip_html(abstracts[0]),source)
    elif abstract:set_field(row,"abstract",abstract.get_text(" ",strip=True),source)
    else:
        heading=next((h for h in soup.find_all(re.compile("^h[1-6]$")) if h.get_text(strip=True)=="Abstract"),None)
        if heading:
            paragraph=heading.find_next_sibling('p')
            if paragraph:set_field(row,"abstract",paragraph.get_text(" ",strip=True),source)
    for value in meta_values(soup,"citation_pdf_url"):add_pdf(row,urljoin(url,value),source)
    for a in soup.find_all("a",href=True):
        if a.get_text(strip=True).lower() in ("pdf","download pdf"):
            add_pdf(row,urljoin(url,a["href"]),source)
    for field in ("citation_keywords","keywords","DC.Subject"):
        add_tags(row,"keyword",meta_values(soup,field),source)
    for node in soup.select('.article__index-terms .organizational-chart a'):
        add_tags(row,'ccs',node.get_text(' ',strip=True),source)
    for node in soup.select('.article__keywords a, .author-keywords a'):
        add_tags(row,'keyword',node.get_text(' ',strip=True),source)
    metadata=page_metadata or {}
    types=meta_values(soup,'dc.Type')+meta_values(soup,'DC.Type')
    if types or metadata.get('dc.Type'):
        row['publication']['publisher_type']=(types[0] if types else metadata['dc.Type']).lower()
    subjects=metadata.get('dc.Subject') or metadata.get('DC.Subject') or metadata.get('citation_keywords')
    if subjects:add_tags(row,'keyword',subjects,source)
    elif metadata.get('keywords'):
        values=metadata['keywords']
        add_tags(row,'keyword',values.split(',') if isinstance(values,str) else values,source)
    dois=meta_values(soup,"citation_doi");row["doi"]=dois[0] if dois else None
    dates=meta_values(soup,"citation_publication_date");row["publication_date"]=dates[0].replace("/","-") if dates else None
    return row
