"""Source-specific collection adapters and metadata enrichment."""
import concurrent.futures
import json
import re
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from urllib.parse import urlencode, urljoin, urlparse
from bs4 import BeautifulSoup
from .http import FetchError
from .model import paper, text, title_key, forum_id, set_field, add_pdf, add_tags, merge_papers, merge_into
from .parsers import parse_acl, parse_miniconf, parse_aaai_page, parse_metadata_html, strip_html, publication_date, researchr_abstract


class Collector:
    def __init__(self,fetch,settings,*,workers=4,as_of,limit=None):
        self.fetch=fetch;self.workers=workers;self.as_of=as_of
        self.settings=settings;self.limit=limit
        self.warnings=[];self.errors=[];self.stats={};self.aaai_sections=None

    def sample(self,rows):
        return rows[:self.limit] if self.limit else rows

    def warn(self,message):
        self.warnings.append(message);print('  WARNING:',message,flush=True)

    def enrich_many(self,rows,fn):
        failures=0
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures={pool.submit(fn,row):row for row in rows}
            for i,f in enumerate(concurrent.futures.as_completed(futures),1):
                row=futures[f]
                try:f.result()
                except (FetchError,ValueError) as e:
                    failures+=1
                    row['errors'].append({'source':row.get('paper_url'),'fields':['abstract','tags','pdf_urls'],'message':str(e)})
                if i%100==0 or i==len(rows):print(f"  details {i}/{len(rows)}, failures={failures}",flush=True)
        return failures

    def miniconf(self,venue,year):
        page=self.settings["catalog_url"].format(venue=venue,year=year)
        html=self.fetch.get(page)
        files=re.findall(r'["\']([^"\']+\.json)["\']',html)
        data_url=next((urljoin(page,x) for x in files if 'orals-posters' in x),None)
        abstracts_url=next((urljoin(page,x) for x in files if 'abstracts' in x),None)
        if not data_url:raise FetchError(f"No official paper JSON found at {page}")
        events=[];url=data_url;visited=set();expected=None
        while url:
            url=url.replace('http://','https://',1)
            if url in visited:raise FetchError(f"Repeated pagination URL: {url}")
            visited.add(url);data=self.fetch.json(url)
            if isinstance(data,list):events.extend(data);break
            if expected is None:expected=data.get('count')
            events.extend(data['results']);url=urljoin(url,data['next']) if data.get('next') else None
        if expected is not None and len(events)!=expected:
            raise FetchError(f"Incomplete official list: expected {expected}, received {len(events)}")
        abstracts={}
        if abstracts_url:
            try:abstracts=self.fetch.json(abstracts_url)
            except FetchError as e:
                if e.status != 'HTTP 404' or not any(x.get('abstract') for x in events):raise
                self.warn(f'{venue} {year}: separate abstract file is absent; using embedded abstracts')
                abstracts_url=None
        rows,excluded=parse_miniconf({'results':events},abstracts,venue,year,data_url,self.settings)
        self.stats['official_events']=len(events);self.stats['excluded_events']=excluded
        rows=merge_papers(rows)
        if not rows:raise FetchError(f"Empty accepted-paper list for {venue} {year}")
        self.stats['official_unique_records']=len(rows)
        for row in rows:
            if abstracts_url and abstracts_url not in row['source_urls']:row['source_urls'].append(abstracts_url)
        return self.sample(rows)

    def paperlists(self,venue,year,rows):
        settings=self.settings.get('paperlists')
        if not settings:return rows
        path=settings['path'].format(venue=venue,year=year);repo=settings['repository'];ref=settings.get('ref','main')
        url=f'https://github.com/{repo}/blob/{ref}/{path}'
        try:data=json.loads(self.fetch.github(repo,path,ref))
        except (FetchError,ValueError) as e:self.warn(str(e));return rows
        by_id=defaultdict(list);by_title=defaultdict(list)
        for x in data:
            if x.get('id'):by_id[str(x['id'])].append(x)
            by_title[title_key(x.get('title'))].append(x)
        count=0
        for row in rows:
            matches=by_id.get(row.get('openreview_id'),[]) or by_title.get(title_key(row['title']),[])
            if len(matches)!=1:continue
            x=matches[0]
            if str(x.get('status','')).casefold() in ('reject','withdraw','withdrawn','desk reject','submitted'):
                row['scope']['snapshot_status']=x.get('status');row['scope']['snapshot_source']=url
                continue
            supplement=paper(venue,year,row['id'],row['title'],url)
            set_field(supplement,'abstract',text(x.get('abstract')),url)
            add_tags(supplement,'keyword',x.get('keywords'),url)
            add_tags(supplement,'primary_area',x.get('primary_area'),url)
            add_tags(supplement,'topic',x.get('topic'),url)
            if x.get('pdf'):add_pdf(supplement,x['pdf'],url)
            if not row.get('openreview_id'):
                supplement['openreview_id']=forum_id(x.get('openreview') or x.get('site'))
            merge_into(row,supplement);count+=1
        self.stats['paperlists_matched']=count
        return rows

    def openreview_enrich(self,venue,year,rows):
        targets=[r for r in rows if r.get('openreview_id') and (not r['abstract'] or not r['pdf_urls'])]
        def enrich(row):
            url='https://api2.openreview.net/notes?'+urlencode({'id':row['openreview_id']})
            notes=self.fetch.json(url).get('notes',[])
            note=next((n for n in notes if n['id']==row['openreview_id']),None)
            if note is None:raise FetchError(f'OpenReview note absent: {row["openreview_id"]}')
            content=note['content']
            def value(key):
                result=content.get(key)
                return result.get('value') if isinstance(result,dict) else result
            if not row['abstract']:set_field(row,'abstract',text(value('abstract')),url)
            for key,kind in [('keywords','keyword'),('primary_area','primary_area'),('subject_area','subject_area')]:
                add_tags(row,kind,value(key),url)
            if value('pdf'):add_pdf(row,urljoin('https://openreview.net/',value('pdf')),url)
        self.stats['openreview_targets']=len(targets)
        self.enrich_many(targets,enrich)
        return rows

    def acl(self,venue,year):
        cfg=self.settings;path=cfg['xml_path'].format(year=year);repo=cfg['repository'];ref=cfg.get('ref','main')
        url=f'https://github.com/{repo}/blob/{ref}/{path}'
        rows=parse_acl(self.fetch.github(repo,path,ref),year,url,venue,cfg['anthology'].format(year=year),cfg['volumes'])
        self.stats['official_unique_records']=len(rows)
        if not rows:raise FetchError('Configured anthology volumes are empty')
        return rows

    def pmlr(self,venue,year):
        volume=self.settings['pmlr_volumes'][str(year)]
        url=f'https://proceedings.mlr.press/v{volume}/';soup=BeautifulSoup(self.fetch.get(url),'html.parser');rows=[]
        for block in soup.select('div.paper'):
            title=block.select_one('.title');link=next((a for a in block.select('a[href]') if a.get_text(strip=True)=='abs'),None)
            if not title or not link:continue
            landing=urljoin(url,link['href']);row=paper(venue,year,landing,title.get_text(' ',strip=True),url)
            row['paper_url']=landing
            authors=block.select_one('.authors');row['authors']=[text(x) for x in authors.get_text().split(',')] if authors else []
            for a in block.select('a[href]'):
                href=urljoin(url,a['href'])
                if '.pdf' in urlparse(href).path:add_pdf(row,href,url)
                if 'openreview.net/forum' in href:row['openreview_id']=forum_id(href)
            row['scope']={'status':'published','track':'proceedings','type':'conference_paper'}
            row['publication']={'pmlr_volume':volume};rows.append(row)
        if not rows:raise FetchError(f'No PMLR paper blocks at {url}')
        self.stats['proceedings_records']=len(rows)
        return rows

    def icml(self,venue,year):
        rows=self.miniconf(venue,year)
        if str(year) in self.settings.get('pmlr_volumes',{}):
            try:published=self.pmlr(venue,year)
            except FetchError as error:
                self.warn(str(error));published=[]
            by_id=defaultdict(list);by_title=defaultdict(list)
            for item in published:
                if item['openreview_id']:by_id[item['openreview_id']].append(item)
                by_title[title_key(item['title'])].append(item)
            for row in rows:
                matches=by_id.get(row['openreview_id'],[]) or by_title.get(title_key(row['title']),[])
                if len(matches)==1:
                    other=matches[0]
                    # A publication URL is an identity alias, never a second candidate list.
                    row['paper_url']=other['paper_url'];merge_into(row,other)
        self.paperlists(venue,year,rows)
        def enrich(row):
            merge_into(row,parse_metadata_html(self.fetch.html(row['paper_url']),venue,year,row['paper_url']))
        self.enrich_many([r for r in rows if not r['abstract']],enrich)
        return self.openreview_enrich(venue,year,rows)

    def cvf(self,venue,year):
        url=self.settings['proceedings_url'].format(venue=venue,year=year);soup=BeautifulSoup(self.fetch.get(url),'html.parser');rows=[]
        for dt in soup.select('dt.ptitle'):
            a=dt.find('a',href=True)
            if not a:continue
            landing=urljoin(url,a['href']);row=paper(venue,year,landing,a.get_text(' ',strip=True),url);row['paper_url']=landing
            for dd in dt.next_siblings:
                if getattr(dd,'name',None)=='dt':break
                if getattr(dd,'name',None)!='dd':continue
                for p in dd.find_all('a',href=True):
                    if p.get_text(strip=True).lower()=='pdf':add_pdf(row,urljoin(url,p['href']),url)
                for author in dd.select('form.authsearch a'):row['authors'].append(author.get_text(' ',strip=True))
            row['scope']={'status':'published','track':'main','type':'conference_paper'};rows.append(row)
        rows=merge_papers(rows)
        if not rows:raise FetchError(f'No CVF paper list at {url}')
        self.stats['official_unique_records']=len(rows)
        rows=self.sample(rows)
        tag_error=None
        try:extra=self.miniconf(venue,year)
        except FetchError as e:self.warn(str(e));extra=[];tag_error=str(e)
        by_title=defaultdict(list)
        for row in extra:by_title[title_key(row['title'])].append(row)
        for row in rows:
            other=by_title.get(title_key(row['title']),[])
            if len(other)==1:merge_into(row,other[0])
        self.paperlists(venue,year,rows)
        if tag_error:
            for row in rows:
                if not row['tags']:
                    row['errors'].append({'source':self.settings['catalog_url'].format(venue=venue,year=year),'fields':['tags'],'message':tag_error})
        def enrich(row):merge_into(row,parse_metadata_html(self.fetch.get(row['paper_url']),venue,year,row['paper_url']))
        self.enrich_many([r for r in rows if not r['abstract']],enrich)
        return rows

    def aaai(self,year):
        base=self.settings['oai_url'];prefixes=tuple(self.settings['section_prefixes']);ns={'o':'http://www.openarchives.org/OAI/2.0/'}
        if self.aaai_sections is None:
            self.aaai_sections={};token=None;seen=set()
            while True:
                params={'verb':'ListSets'}
                if token:params['resumptionToken']=token
                root=ET.fromstring(self.fetch.get(base+'?'+urlencode(params)))
                for s in root.findall('.//o:set',ns):
                    self.aaai_sections[s.findtext('o:setSpec',namespaces=ns)]=s.findtext('o:setName',namespaces=ns)
                token=root.findtext('.//o:resumptionToken',namespaces=ns)
                if not token:break
                if token in seen:raise FetchError('OAI repeated a section-list continuation token')
                seen.add(token)
        sections={k:v for k,v in self.aaai_sections.items() if f':AI{year%100}-' in k and v.startswith(prefixes)}
        if not sections:raise FetchError(f'No AAAI technical sections found for {year}')
        rows=[];pages=0;ignored=0;completed=0
        for section in sorted(sections):
            token=None;seen=set()
            try:
                while True:
                    params={'verb':'ListRecords','resumptionToken':token} if token else {'verb':'ListRecords','metadataPrefix':'oai_dc','set':section}
                    url=base+'?'+urlencode(params)
                    batch,next_token,n=parse_aaai_page(self.fetch.get(url),{year},url,self.aaai_sections,prefixes)
                    rows.extend(batch);ignored+=n;pages+=1
                    if self.limit and len(rows)>=self.limit:
                        self.stats["sampled_pages"]=pages
                        return self.sample(merge_papers(rows))
                    if not next_token:break
                    if next_token in seen:raise FetchError('OAI repeated a record continuation token')
                    seen.add(next_token);token=next_token
                    time.sleep(.15)
                completed+=1
            except FetchError as e:
                self.errors.append({'source':section,'message':str(e)})
            print(f'  AAAI {year} sections={completed}/{len(sections)}, papers={len(rows)}',flush=True)
        rows=merge_papers(rows)
        self.stats.update(oai_pages=pages,excluded_records=ignored,sections_expected=len(sections),sections_completed=completed)
        self.stats['official_unique_records']=len(rows)
        if not rows:raise FetchError(f'No AAAI technical papers for {year}')
        return rows

    def jmlr(self,venue,year):
        vol=year-self.settings["volume_year_offset"];url=f'https://jmlr.org/papers/v{vol}/';soup=BeautifulSoup(self.fetch.get(url),'html.parser');rows=[];seen=set()
        for a in soup.find_all('a',href=True):
            link=urljoin(url,a['href'])
            if not re.search(rf'/papers/v{vol}/[^/]+\.html$',link) or link in seen:continue
            seen.add(link)
            row=paper(venue,year,link,a.get_text(' ',strip=True),url);row['paper_url']=link
            row['publication']={'volume':str(vol)};row['scope']={'status':'published','type':'journal_article'};rows.append(row)
        if not rows:raise FetchError(f'Empty JMLR volume: {url}')
        self.stats['official_unique_records']=len(rows)
        def enrich(row):
            detail=parse_metadata_html(self.fetch.get(row['paper_url']),venue,year,row['paper_url'])
            if detail['title']:row['title']=detail['title']
            merge_into(row,detail)
        rows=self.sample(rows)
        self.enrich_many(rows,enrich)
        return [r for r in rows if not r['publication_date'] or r['publication_date']<=self.as_of]

    def crossref(self,venue,year):
        # Crossref queries may return a DOI under multiple print/online years.
        base=f'https://api.crossref.org/journals/{self.settings["issn"]}/works';cursor='*';seen=set();rows=[];total=None;received=0
        while True:
            params={'filter':f'from-pub-date:{year}-01-01,until-pub-date:{min(str(year)+"-12-31",self.as_of)}','rows':100,'cursor':cursor}
            url=base+'?'+urlencode(params);msg=self.fetch.json(url)['message']
            if total is None:total=msg['total-results']
            items=msg['items'];received+=len(items)
            for item in items:
                date=publication_date(item)
                if not date or date[:4]!=str(year) or date>self.as_of:continue
                if item.get('type')!='journal-article':continue
                doi=item['DOI'].lower();row=paper(venue,year,doi,' '.join(item.get('title',[])),url)
                row['doi']=doi;row['paper_url']=self.settings['publisher_url'].format(doi=doi)
                row['publication_date']=date;row['publication']={'journal':venue,'volume':item.get('volume'),'issue':item.get('issue'),'published_online':item.get('published-online'),'published_print':item.get('published-print')}
                row['authors']=[text(' '.join([a.get('given',''),a.get('family','')])) for a in item.get('author',[])]
                set_field(row,'abstract',strip_html(item.get('abstract')),url)
                add_tags(row,'subject',item.get('subject'),url)
                add_pdf(row,self.settings['pdf_url'].format(doi=doi),url,derived=True)
                row['scope']={'status':'published','type':'journal_article'};rows.append(row)
            if self.limit and len(rows)>=self.limit:break
            if not items or received>=total:break
            nxt=msg.get('next-cursor')
            if not nxt or nxt in seen:raise FetchError('Crossref pagination did not advance')
            seen.add(nxt);cursor=nxt;time.sleep(1)
        if received<total and not self.limit:raise FetchError(f'Crossref incomplete: {received}/{total}')
        self.stats['crossref_records']=received;self.stats['excluded_date_or_type']=received-len(rows)
        rows=self.sample(merge_papers(rows))
        def enrich(row):
            detail=parse_metadata_html(self.fetch.html(row['paper_url'],force_remote=self.fetch.remote),venue,year,row['paper_url'],page_metadata=self.fetch.page_metadata(row['paper_url']))
            if not detail['abstract'] and not detail['tags'] and not detail['publication'].get('publisher_type'):
                raise FetchError(f'Publisher metadata not readable: {row["paper_url"]}')
            merge_into(row,detail)
        self.enrich_many(rows,enrich)
        excluded=self.settings.get('excluded_publication_types',[])
        self.stats['excluded_publication_types']=sum(r['publication'].get('publisher_type') in excluded for r in rows)
        for row in rows:
            if excluded and not row['publication'].get('publisher_type'):
                row['scope']['publication_type_status']='unknown'
        self.stats['unknown_publication_type']=sum(not r['publication'].get('publisher_type') for r in rows)
        rows=[r for r in rows if r['publication'].get('publisher_type') not in excluded]
        return rows

    def researchr(self,venue,year):
        url=self.settings['researchr'].get(str(year))
        if not url:raise ValueError(f'Researchr edition URL missing for {venue} {year}')
        track=url.rstrip('/').rsplit('/',1)[-1]
        soup=BeautifulSoup(self.fetch.html(url),'html.parser')
        section=soup.select_one('#event-overview')
        if not section:raise FetchError(f'Accepted paper section is absent: {url}')
        form=soup.select_one('#event-modal-loader form')
        if not form:raise FetchError(f'Public detail form is absent: {url}')
        params={x['name']:x.get('value','') for x in form.select('input[name]')}
        input_name=form.select_one('.event-id-input')['name'];action=form.select_one('[submitid]')['submitid']
        params[action]='1';params['__ajax_runtime_request__']='event-modal-loader'
        endpoint=urljoin(url,form['action']);rows=[]

        def links(row,node,source,selector='a[href]'):
            for a in node.select(selector):
                href=urljoin(url,a['href'].strip());label=a.get_text(' ',strip=True).lower()
                if 'publication-link' in (a.get('class') or []):
                    item={'label':a.get_text(' ',strip=True),'url':href,'source':source}
                    if item not in row['publication'].setdefault('links',[]):row['publication']['links'].append(item)
                if 'doi.org/10.' in href:
                    doi=urlparse(href).path.lstrip('/').rstrip('/')
                    if doi.lower().startswith('10.48550/arxiv.'):
                        row['publication']['preprint_doi']=doi
                        add_pdf(row,'https://arxiv.org/pdf/'+doi.split('.',2)[-1],source,derived=True)
                    elif doi.lower().startswith('10.5281/zenodo.'):
                        row['publication']['repository_doi']=doi
                    else:row['doi']=doi
                elif 'dl.acm.org/doi/' in href:
                    row['doi']=href.split('/doi/',1)[1].removeprefix('abs/').removeprefix('full/').removeprefix('pdf/').split('?')[0]
                if '.pdf' in urlparse(href).path.lower() and not any(x in label for x in ['slide','poster']):add_pdf(row,href,source)
                if 'arxiv.org/abs/' in href:add_pdf(row,href.replace('/abs/','/pdf/'),source,derived=True)
                if 'arxiv.org/pdf/' in href:add_pdf(row,href,source)
                if '/details/' in href and f'/{track}/' in href:row['paper_url']=href
            if row.get('doi','') and row['doi'].lower().startswith('10.1145/'):
                add_pdf(row,'https://dl.acm.org/doi/pdf/'+row['doi'],source,derived=True)

        for tr in section.select('tr'):
            anchor=tr.select_one('a[data-event-modal]')
            if not anchor:continue
            for badge in anchor.select('.output-badge'):badge.decompose()
            eid=anchor['data-event-modal'];row=paper(venue,year,eid,anchor.get_text(' ',strip=True),url)
            row['paper_url']=url+'#'+eid
            row['authors']=[a.get_text(' ',strip=True) for a in tr.select('.performers a')]
            row['scope']={'status':'accepted','track':'research','type':'conference_paper'}
            row['publication']={'researchr_event_id':eid}
            links(row,tr,url);rows.append(row)
        rows=merge_papers(rows)
        if not rows:raise FetchError(f'Accepted paper table is empty: {url}')
        self.stats['official_unique_records']=len(rows)

        def enrich(row):
            eid=row['publication']['researchr_event_id'];p=dict(params);p[input_name]=eid
            cache_key=url+'#metadata-'+eid
            response=json.loads(self.fetch.read_form(endpoint,p,cache_key))
            bodies=[x.get('value','') for x in response if x.get('action')=='append' and x.get('id')=='event-modals']
            if not bodies:raise FetchError(f'No paper detail in modal response: {cache_key}')
            detail=BeautifulSoup(''.join(bodies),'html.parser');desc=detail.select_one('.event-description')
            if desc:
                abstract=researchr_abstract(desc)
                set_field(row,'abstract',text(abstract),cache_key)
            header=detail.select_one('.modal-header')
            if header:
                for a in header.select('a[href]'):
                    if '#program' in a['href']:add_tags(row,'session',a.get_text(' ',strip=True),cache_key)
            links(row,detail,cache_key,'.publication-link[href], .modal-footer a[href]')
            if row['paper_url'] not in row['source_urls']:row['source_urls'].append(row['paper_url'])
            if desc is None:raise FetchError(f'No description container in the paper detail response: {cache_key}')
            # Retrieve author-supplied attachments only when no PDF link is available yet.
            if not row['pdf_urls'] and '/details/' in row['paper_url']:
                page=BeautifulSoup(self.fetch.html(row['paper_url']),'html.parser')
                # The later session program contains other papers' publication links.
                for label in page.find_all('label'):
                    if label.get_text(' ',strip=True) in ('Link to Publication','Pre-print','DOI','File attachments'):
                        container=label.parent
                        if 'presentation' not in container.get_text(' ',strip=True).lower():
                            links(row,container,row['paper_url'])
        rows=self.sample(rows)
        self.enrich_many(rows,enrich)
        return self.bibliography_enrich(venue,year,rows)

    def bibliography_enrich(self,venue,year,rows):
        """Match existing research papers to DBLP; never expand the accepted list."""
        if not self.settings.get('bibliography_enrichment'):return rows
        from .dblp import DblpCollector
        settings={**self.settings,**self.settings.get('overrides',{}).get(str(year),{})}
        source='https://dblp.org/search/publ/api?'+urlencode({'q':f'stream:{settings["stream"]}: year:{year}:'})
        try:
            records=DblpCollector(stream=settings['stream'],year=year,issue=settings.get('issue'),client=self.fetch.client).collect(None,100)
        except (FetchError,ValueError) as error:
            self.warn(str(error));self.stats['bibliography_enrichment']='unavailable';return rows
        hits=[({'title':r['title'],'year':r['year'],'key':r['dblp_id'],'doi':re.sub(r'^https?://(?:dx[.])?doi[.]org/', '',r.get('doi_url') or ''),'ee':r.get('document_urls',[])},source) for r in records]
        index=defaultdict(list)
        for item,source in hits:
            if str(item.get('year'))==str(year):index[title_key(item.get('title'))].append((item,source))
        matched=0
        for row in rows:
            candidates=index.get(title_key(row['title']),[])
            if len(candidates)!=1:continue
            item,source=candidates[0];doi=item.get('doi')
            if doi and row['doi'] and row['doi'].lower()!=doi.lower():
                self.warn(f'DOI disagreement for {row["id"]}; keeping the conference source');continue
            matched+=1
            if doi:set_field(row,'doi',doi,source)
            row['publication']['dblp_key']=item['key']
            if source not in row['source_urls']:row['source_urls'].append(source)
            links=item.get('ee',[])
            if isinstance(links,str):links=[links]
            for link in links:
                entry={'label':'Publication','url':link,'source':source}
                if entry not in row['publication'].setdefault('links',[]):row['publication']['links'].append(entry)
                if urlparse(link).path.lower().endswith('.pdf') or 'arxiv.org/pdf/' in link:add_pdf(row,link,source)
                elif 'arxiv.org/abs/' in link:add_pdf(row,link.replace('/abs/','/pdf/'),source,derived=True)
            if doi and doi.startswith('10.1145/'):
                add_pdf(row,'https://dl.acm.org/doi/pdf/'+doi,source,derived=True)
        self.stats['bibliography_records']=len(hits);self.stats['bibliography_matched']=matched
        return rows

    def collect(self,venue,year):
        self.warnings=[];self.errors=[];self.stats={}
        adapter=self.settings['adapter']
        if adapter=='miniconf':
            rows=self.openreview_enrich(venue,year,self.paperlists(venue,year,self.miniconf(venue,year)))
        elif adapter=='aaai':rows=self.aaai(year)
        elif adapter in ('acl','icml','cvf','jmlr','crossref','researchr'):
            rows=getattr(self,adapter)(venue,year)
        else:raise ValueError(f'Unknown platform adapter: {adapter}')
        return self.sample(merge_papers(rows))
