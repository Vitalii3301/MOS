from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .util import utcnow


_URL_RE = re.compile(r"https://[^\s<>\"']+", re.IGNORECASE)


class _ValidatedRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, validator):
        self.validator = validator

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.validator(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SafeURLResearchAdapter:
    """Fetch evidence only from explicit public HTTPS URLs in the query.

    This is a fetcher, not a search engine. It blocks credentials in URLs,
    non-HTTPS schemes, localhost/private IPs, large bodies and binary content.
    Every successful network result carries an HTTP receipt and content hash.
    """

    def __init__(self, *, allowlist: list[str] | None = None, timeout: float = 15.0,
                 max_bytes: int = 512 * 1024, max_urls: int = 3):
        self.allowlist={x.strip().lower().lstrip(".") for x in (allowlist or []) if x.strip()}
        self.timeout=max(1.0,min(float(timeout),60.0))
        self.max_bytes=max(4096,min(int(max_bytes),2*1024*1024))
        self.max_urls=max(1,min(int(max_urls),5))
        self.opener=urllib.request.build_opener(_ValidatedRedirect(self._validate_url))

    def _validate_url(self,url:str)->urllib.parse.SplitResult:
        parsed=urllib.parse.urlsplit(url)
        if parsed.scheme.lower()!="https":raise ValueError("research URL must use https")
        if parsed.username or parsed.password:raise ValueError("credentials in research URL are forbidden")
        host=(parsed.hostname or "").rstrip(".").lower()
        if not host:raise ValueError("research URL has no host")
        allowlisted=bool(self.allowlist and any(host==allowed or host.endswith("."+allowed) for allowed in self.allowlist))
        if self.allowlist and not allowlisted:
            raise ValueError(f"research host is not allowlisted: {host}")
        # An explicit allowlist is also the safe route in managed environments
        # whose HTTPS proxy resolves DNS remotely.
        if allowlisted:return parsed
        try:addresses={row[4][0] for row in socket.getaddrinfo(host,parsed.port or 443,type=socket.SOCK_STREAM)}
        except socket.gaierror as exc:raise ValueError(f"research host DNS lookup failed: {host}") from exc
        if not addresses:raise ValueError("research host did not resolve")
        for raw in addresses:
            ip=ipaddress.ip_address(raw)
            if not ip.is_global:raise ValueError(f"research host resolves to a non-public address: {ip}")
        return parsed

    def _urls(self,query:str)->list[str]:
        urls=[]
        for raw in _URL_RE.findall(query):
            url=raw.rstrip(".,;:!?)]}")
            if url not in urls:
                self._validate_url(url);urls.append(url)
            if len(urls)>=self.max_urls:break
        return urls

    def fetch(self,query:str)->list[dict[str,Any]]:
        urls=self._urls(query)
        if not urls:
            return [{"kind":"research_boundary","status":"no_explicit_https_url","network_used":False,
                     "claim":"No network request was made; this adapter fetches explicit HTTPS URLs only."}]
        out=[]
        for url in urls:
            started=time.monotonic()
            try:
                req=urllib.request.Request(url,headers={"User-Agent":"MOS-Verified-Research/4.1","Accept":"text/*,application/json"})
                with self.opener.open(req,timeout=self.timeout) as response:
                    final_url=response.geturl();self._validate_url(final_url)
                    content_type=response.headers.get_content_type()
                    if not (content_type.startswith("text/") or content_type in {"application/json","application/ld+json","application/xml"}):
                        raise ValueError(f"unsupported research content type: {content_type}")
                    declared=response.headers.get("Content-Length")
                    if declared and int(declared)>self.max_bytes:raise ValueError("research response exceeds byte limit")
                    raw=response.read(self.max_bytes+1)
                    if len(raw)>self.max_bytes:raise ValueError("research response exceeds byte limit")
                    charset=response.headers.get_content_charset() or "utf-8"
                    text=raw.decode(charset,errors="replace")
                    clean=re.sub(r"\s+"," ",re.sub(r"<[^>]{1,500}>"," ",text)).strip()
                    out.append({"kind":"network_evidence","status":"fetched","source_url":url,"final_url":final_url,
                                "http_status":getattr(response,"status",200),"content_type":content_type,
                                "bytes":len(raw),"content_sha256":hashlib.sha256(raw).hexdigest(),
                                "excerpt":clean[:4000],"fetched_at":utcnow(),"latency_ms":round((time.monotonic()-started)*1000,2),
                                "network_used":True})
            except (urllib.error.URLError,TimeoutError,ValueError,OSError) as exc:
                out.append({"kind":"network_error","status":"failed","source_url":url,"error":f"{type(exc).__name__}: {exc}",
                            "fetched_at":utcnow(),"latency_ms":round((time.monotonic()-started)*1000,2),"network_used":False})
        return out
