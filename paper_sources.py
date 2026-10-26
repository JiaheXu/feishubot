"""Extract public paper links and retrieve readable HTML/PDF content."""

import base64
from datetime import datetime
import hashlib
from html.parser import HTMLParser
import http.client
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import tempfile
import unicodedata
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo


MAX_BYTES = 20 * 1024 * 1024
MAX_TEXT = 100_000


def search_papers(query):
    url = "https://export.arxiv.org/api/query?" + urlencode({
        "search_query": query, "max_results": 5, "sortBy": "relevance"})
    try:
        body, _, _ = download(url)
        root = ElementTree.fromstring(body)
    except (OSError, ValueError, RuntimeError, http.client.HTTPException,
            ElementTree.ParseError) as error:
        # A second index avoids depending on arXiv's rate-limited search API.
        title = re.fullmatch(r'ti:"([^"]+)"', query.strip())
        fallback_url = "https://huggingface.co/api/papers/search?" + urlencode({
            "q": title.group(1) if title else query})
        try:
            body, _, _ = download(fallback_url)
            entries = json.loads(body)
            if not isinstance(entries, list):
                raise ValueError("Invalid paper search response.")
            return [{"title": entry["paper"]["title"],
                     "url": "https://arxiv.org/abs/" + quote(entry["paper"]["id"], safe=""),
                     "abstract": entry["paper"].get("summary", "")[:4000],
                     "authors": [author["name"] for author in entry["paper"].get("authors", [])]}
                    for entry in entries[:5]]
        except (OSError, ValueError, RuntimeError, http.client.HTTPException,
                KeyError, TypeError) as fallback_error:
            raise RuntimeError(f"arXiv search failed ({error}); Hugging Face fallback failed ({fallback_error}).") from None
    ns = {"a": "http://www.w3.org/2005/Atom"}
    return [{"title": " ".join(entry.findtext("a:title", "", ns).split()),
             "url": entry.findtext("a:id", "", ns).replace("http://", "https://", 1),
             "abstract": entry.findtext("a:summary", "", ns)[:4000],
             "authors": [author.findtext("a:name", "", ns) for author in entry.findall("a:author", ns)]}
            for entry in root.findall("a:entry", ns)]


class _SearchResults(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and "result__a" in attrs.get("class", "").split():
            href = attrs.get("href", "")
            href = parse_qs(urlsplit(href).query).get("uddg", [href])[0]
            self.current = {"title": "", "url": href}

    def handle_data(self, data):
        if self.current is not None:
            self.current["title"] += data

    def handle_endtag(self, tag):
        if tag == "a" and self.current is not None:
            if self.current["url"].startswith(("https://", "http://")):
                self.results.append(self.current)
            self.current = None


def search_web(query):
    # shortcut: public search can challenge automated clients; use a search API if reliability requires it.
    body, _, _ = download("https://html.duckduckgo.com/html/?" + urlencode({"q": query}))
    page = _SearchResults()
    page.feed(body.decode("utf-8", errors="replace"))
    if not page.results:
        raise RuntimeError("No readable web search results; search may be blocked or require a challenge.")
    return page.results[:5]


def extract_links(content):
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            pass
    strings = []

    def collect(value):
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(content)
    urls = []
    for string in strings:
        for match in re.findall(r'https?://[^\s<>"\'\\]+', string):
            url = match.rstrip(".,;:!?)]}，。；：！？）》】")
            if url not in urls:
                urls.append(url)
    return urls


def public_target(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Only public HTTP(S) source URLs are supported.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Source URLs must not contain credentials.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = [ipaddress.ip_address(parsed.hostname)]
    except ValueError:
        addresses = [ipaddress.ip_address(item[4][0]) for item in
                     socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)]
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("Local, private, and reserved source addresses are blocked.")
    return parsed, str(addresses[0])


def _source_socket(parsed, address, proxy_url):
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not proxy_url:
        return socket.create_connection((address, port), timeout=30)
    proxy = urlsplit(proxy_url)
    if proxy.scheme != "http" or not proxy.hostname:
        raise ValueError("LOCAL_HTTP_PROXY must be an HTTP CONNECT proxy URL.")
    headers = {}
    if proxy.username is not None:
        credentials = f"{unquote(proxy.username)}:{unquote(proxy.password or '')}"
        headers["Proxy-Authorization"] = "Basic " + base64.b64encode(credentials.encode()).decode()
    tunnel = http.client.HTTPConnection(proxy.hostname, proxy.port or 80, timeout=30)
    try:
        tunnel.set_tunnel(address, port, headers=headers)
        tunnel.connect()
        source_socket = tunnel.sock
        tunnel.sock = None
        return source_socket
    finally:
        tunnel.close()


def _request(parsed, address):
    host = parsed.hostname.encode("idna").decode("ascii")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    secure = parsed.scheme == "https"
    connection_type = http.client.HTTPSConnection if secure else http.client.HTTPConnection
    connection = connection_type(host, port, timeout=30)
    try:
        # Connect to the validated IP, retaining the original TLS name and Host header.
        connection.sock = _source_socket(parsed, address, os.environ.get("LOCAL_HTTP_PROXY"))
        if secure:
            connection.sock = ssl.create_default_context().wrap_socket(
                connection.sock, server_hostname=host)
        target = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
        if parsed.query:
            target += "?" + quote(parsed.query, safe="%=&?/:@!$'()*+,;~-._")
        connection.request("GET", target, headers={
            "User-Agent": "FeishuPaperMonitor/1.0", "Accept-Encoding": "identity",
            "Connection": "close",
        })
        with connection.getresponse() as response:
            headers = {key.lower(): value for key, value in response.getheaders()}
            if int(headers.get("content-length", "0")) > MAX_BYTES:
                raise RuntimeError("Source exceeds the 20 MiB download limit.")
            body = response.read(MAX_BYTES + 1) if response.status == 200 else b""
            if len(body) > MAX_BYTES:
                raise RuntimeError("Source exceeds the 20 MiB download limit.")
            return response.status, headers, body
    finally:
        connection.close()


def download(url):
    for _ in range(6):
        parsed, address = public_target(url)
        status, headers, body = _request(parsed, address)
        if status in (301, 302, 303, 307, 308) and headers.get("location"):
            url = urljoin(url, headers["location"])
            continue
        if status != 200:
            raise RuntimeError(f"Source returned HTTP {status}.")
        return body, headers.get("content-type", ""), url
    raise RuntimeError("Source exceeded the redirect limit.")


class _PageText(HTMLParser):
    ignored_tags = {"script", "style", "noscript", "svg", "nav", "footer"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ignored = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self.ignored_tags:
            self.ignored += 1

    def handle_endtag(self, tag):
        if tag in self.ignored_tags:
            self.ignored = max(0, self.ignored - 1)

    def handle_data(self, data):
        if not self.ignored and data.strip():
            self.parts.append(data.strip())


def _extract(body, content_type):
    if body.startswith(b"%PDF-") or "application/pdf" in content_type:
        try:
            from pypdf import PdfReader
        except ImportError:
            raise RuntimeError("PDF extraction requires pypdf; install requirements.txt.") from None
        reader = PdfReader(io.BytesIO(body))
        parts, size = [], 0
        for index, page in enumerate(reader.pages):
            if index >= 150 or size >= MAX_TEXT:
                break
            text = f"[Page {index + 1}]\n{page.extract_text() or ''}"
            parts.append(text)
            size += len(text)
        # Page markers alone are not readable paper content (e.g. scanned PDFs).
        text = "\n\n".join(parts)
        if not re.sub(r"\[Page \d+\]", "", text).strip():
            raise RuntimeError("PDF has no extractable text; scanned PDFs require OCR.")
        return text[:MAX_TEXT], len(parts) < len(reader.pages) or len(text) > MAX_TEXT
    if "html" in content_type or body.lstrip().startswith(b"<"):
        charset = re.search(r"charset=[\"']?([^\s;\"']+)", content_type, re.I)
        page = _PageText()
        page.feed(body.decode(charset.group(1) if charset else "utf-8", errors="replace"))
        text = "\n".join(page.parts)
    elif content_type.startswith("text/plain"):
        text = body.decode("utf-8", errors="replace")
    else:
        raise RuntimeError("Source is not a supported HTML, plain-text, or PDF document.")
    if not text.strip():
        raise RuntimeError("Source has no readable text; it may require login or JavaScript.")
    return text[:MAX_TEXT], len(text) > MAX_TEXT


def arxiv_id(url):
    parsed = urlsplit(url)
    if parsed.hostname in ("arxiv.org", "www.arxiv.org", "export.arxiv.org"):
        match = re.fullmatch(r"/(?:abs|pdf|html)/(.+?)(?:\.pdf)?", parsed.path)
        if match:
            return match.group(1)


class _PaperMetadata(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.title = ""
        self.in_title = False
        self.link = None
        self.paper_urls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            self.meta[(attrs.get("name") or attrs.get("property") or "").lower()] = attrs.get("content", "")
        elif tag == "a":
            self.link = [attrs.get("href", ""), "", attrs.get("aria-label") or attrs.get("title") or ""]

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.link is not None:
            self.link[1] += data

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        elif tag == "a" and self.link is not None:
            label = re.sub(r"[\W_]+", " ", (self.link[1].strip() or self.link[2]).casefold()).strip()
            if label in (
                    "paper", "pdf", "read paper", "read the paper", "download paper", "download pdf",
                    "technical report", "report", "full paper", "paper pdf", "arxiv", "paper arxiv",
                    "arxiv paper", "论文", "论文 pdf", "阅读论文", "技术报告"):
                self.paper_urls.append(self.link[0])
            self.link = None


def source_metadata(body, content_type, url):
    metadata = {}
    if not body.startswith(b"%PDF-") and ("html" in content_type or body.lstrip().startswith(b"<")):
        page = _PaperMetadata()
        page.feed(body.decode("utf-8", errors="replace"))
        metadata["title"] = page.meta.get("citation_title") or page.title.strip()
        date = page.meta.get("citation_publication_date") or page.meta.get("citation_date") or ""
        year = re.search(r"\b(?:19|20)\d{2}\b", date)
        if year:
            metadata["year"] = year.group()
        if page.meta.get("citation_pdf_url"):
            metadata["pdf_url"] = urljoin(url, page.meta["citation_pdf_url"])
        metadata["paper_urls"] = list(dict.fromkeys(urljoin(url, link) for link in page.paper_urls))[:10]
    elif body.startswith(b"%PDF-"):
        metadata["pdf_url"] = url
    identifier = arxiv_id(url)
    if identifier:
        metadata["pdf_url"] = f"https://arxiv.org/pdf/{identifier}"
        metadata["paper_urls"] = []
        match = re.match(r"(\d{2})\d{2}\.", identifier)
        if match:
            metadata["year"] = str(2000 + int(match.group(1)))
    return metadata


def archive_papers(source, directory):
    urls = []
    if source.get("pdf_url"):
        urls.append(source["pdf_url"])
    elif arxiv_id(source["url"]) or urlsplit(source["url"]).path.lower().endswith(".pdf"):
        urls.append(source["url"])
    urls.extend(source.get("paper_urls") or [])
    saved, errors = [], []
    for url in list(dict.fromkeys(urls))[:10]:
        try:
            if url in (source["url"], source.get("pdf_url")):
                paper = source
            elif urlsplit(url).path.lower().endswith(".pdf"):
                paper = {"url": url}
            else:
                paper = fetch_source(url)
            identifier = arxiv_id(url)
            pdf_url = f"https://arxiv.org/pdf/{identifier}" if identifier else paper.get("pdf_url", url)
            body, _, final_url = download(pdf_url)
            if not body.startswith(b"%PDF-"):
                raise RuntimeError("The download is not a PDF document.")
            title = paper.get("title")
            if not title:
                from pypdf import PdfReader
                metadata = PdfReader(io.BytesIO(body)).metadata
                title = metadata.title if metadata else None
            title = title or unquote(Path(urlsplit(final_url).path).stem)
            slug = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", title)
                          .encode("ascii", "ignore").decode().lower()).strip("-")[:160].rstrip("-") or "paper"
            year = paper.get("year") or source_metadata(body, "application/pdf", pdf_url).get("year")
            if not re.fullmatch(r"(?:19|20)\d{2}", str(year)):
                year = str(datetime.now(ZoneInfo("Asia/Shanghai")).year)
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            digest = hashlib.sha256(body).hexdigest()[:12]
            for name in (f"{year}-{slug}.pdf", f"{year}-{slug}-{digest}.pdf"):
                path = directory / name
                with tempfile.NamedTemporaryFile(dir=directory, delete=False) as output:
                    temporary = Path(output.name)
                    output.write(body)
                try:
                    try:
                        os.link(temporary, path)
                    except FileExistsError:
                        if path.is_symlink() or path.read_bytes() != body:
                            continue
                    saved.append(str(path))
                    break
                finally:
                    temporary.unlink()
            else:
                raise RuntimeError("PDF filename collision; existing files were preserved.")
        except Exception as error:
            errors.append(f"{url}: {error}")
    return saved, errors


def fetch_source(url):
    candidates = []
    paper_id = arxiv_id(url)
    if paper_id:
        candidates.extend([f"https://arxiv.org/html/{paper_id}",
                           f"https://arxiv.org/pdf/{paper_id}"])
    candidates.append(url)
    failures = []
    for candidate in dict.fromkeys(candidates):
        try:
            body, content_type, final_url = download(candidate)
            text, truncated = _extract(body, content_type)
            return {"url": url, "retrieved_url": final_url, "text": text,
                    "truncated": truncated, **source_metadata(body, content_type, final_url)}
        except (OSError, ValueError, RuntimeError, http.client.HTTPException) as error:
            failures.append(str(error))
    raise RuntimeError("Could not read source: " + "; ".join(failures))
