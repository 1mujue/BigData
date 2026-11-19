"""Acquire public source text independently of the model provider."""

from html.parser import HTMLParser
from ipaddress import ip_address
import socket
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from ..pdfs.pages import read_pdf_pages


def public_url(url):
    parts = urlsplit(url)
    if (parts.scheme not in {'http', 'https'} or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.port not in {None, 80, 443}):
        raise ValueError('expected a public HTTP(S) source URL')
    addresses = socket.getaddrinfo(parts.hostname, parts.port, type=socket.SOCK_STREAM)
    if not all(ip_address(address[4][0]).is_global for address in addresses):
        raise ValueError('source URL resolves to a non-public address')
    return parts._replace(fragment='').geturl()


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, target):
        return super().redirect_request(request, response, code, message, headers, public_url(target))


class VisibleText(HTMLParser):
    hidden = {'script', 'style', 'template', 'svg', 'noscript'}
    blocks = {'article', 'br', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'li', 'p', 'pre', 'section', 'td', 'th'}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.skipped = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.hidden:
            self.skipped += 1
        elif tag in self.blocks and not self.skipped:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in self.hidden and self.skipped:
            self.skipped -= 1
        elif tag in self.blocks and not self.skipped:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.skipped:
            self.parts.append(data)

    def text(self):
        return '\n'.join(line.strip() for line in ''.join(self.parts).splitlines() if line.strip())


def pdf_text(body):
    return '\n\n'.join(f'[PDF page {index}]\n{text}' for index, text in enumerate(read_pdf_pages(body), 1))


def fetch(url, timeout, expect_pdf=False):
    url = public_url(url)
    parts = urlsplit(url)
    path = parts.path.split('/')
    if parts.hostname == 'github.com' and len(path) >= 6 and path[3] == 'blob':
        # GitHub's rendered code view may omit file text from its HTML.
        url = urlunsplit(('https', 'raw.githubusercontent.com', '/'.join(path[:3] + path[4:]), '', ''))
        url = public_url(url)

    request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    opener = urllib.request.build_opener(PublicRedirect())
    limit = 25 * 1024 * 1024
    with opener.open(request, timeout=timeout) as response:
        content_type = response.headers.get_content_type()
        encoding = response.headers.get_content_charset() or 'utf-8'
        body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError('source exceeds 25 MiB')
    if body.startswith(b'%PDF-'):
        return 'paper', pdf_text(body)
    if expect_pdf:
        raise ValueError('PDF URL did not return a PDF')

    host = parts.hostname
    if host in {'github.com', 'raw.githubusercontent.com', 'gitlab.com', 'anonymous.4open.science'}:
        kind = 'repository'
    elif host == 'arxiv.org' and parts.path.startswith('/html/'):
        kind = 'paper'
    else:
        kind = 'data'
    text = body.decode(encoding, errors='replace')
    if content_type in {'text/html', 'application/xhtml+xml'}:
        page = VisibleText()
        page.feed(text)
        text = page.text()
    elif not (content_type.startswith('text/') or content_type == 'application/json'):
        raise ValueError('source did not return supported text')
    if not text.strip():
        raise ValueError('source returned no text')
    return kind, text
