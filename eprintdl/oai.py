from __future__ import annotations

import urllib.parse
import xml.etree.ElementTree as ET
from typing import Callable, Iterator

from .http import FetchError, fetch as http_fetch
from .utils import BASE_URL, RateLimiter

OAI_ENDPOINT = f"{BASE_URL}/oai"
NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/",
    "dc": "http://purl.org/dc/elements/1.1/",
}
OAI_NS = "{%s}" % NS["oai"]
DC_NS = "{%s}" % NS["dc"]

ID_PREFIX = "oai:eprint.iacr.org:"


class OaiError(RuntimeError):
    pass


class OaiClient:

    def __init__(self, limiter: RateLimiter | None = None, retries: int = 4) -> None:
        self.limiter = limiter or RateLimiter(1.0)
        self.retries = retries

    def _fetch(self, params: dict[str, str]) -> bytes:
        url = f"{OAI_ENDPOINT}?{urllib.parse.urlencode(params)}"
        try:
            raw, _ = http_fetch(
                url,
                limiter=self.limiter,
                headers={"Accept": "application/xml,text/xml;q=0.9,*/*;q=0.8"},
                retries=self.retries,
            )
            return raw
        except FetchError as exc:
            raise OaiError(f"OAI 请求失败: {exc}") from exc

    def list_records(
        self,
        *,
        date_from: str | None = None,
        date_until: str | None = None,
        resumption_token: str | None = None,
    ) -> tuple[list[dict], str | None]:
        if resumption_token:
            params = {"verb": "ListRecords", "resumptionToken": resumption_token}
        else:
            params = {"verb": "ListRecords", "metadataPrefix": "oai_dc"}
            if date_from:
                params["from"] = date_from
            if date_until:
                params["until"] = date_until
        raw = self._fetch(params)
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise OaiError(f"OAI 响应解析失败: {exc}") from exc

        error = root.find(f"{OAI_NS}error")
        if error is not None:
            code = error.get("code", "")
            if code == "noRecordsMatch":
                return [], None
            raise OaiError(f"OAI 错误 [{code}]: {(error.text or '').strip()}")

        list_records = root.find(f"{OAI_NS}ListRecords")
        if list_records is None:
            raise OaiError("OAI 响应缺少 ListRecords 节点")

        records = [self._parse_record(rec) for rec in list_records.findall(f"{OAI_NS}record")]
        records = [r for r in records if r is not None]
        token_el = list_records.find(f"{OAI_NS}resumptionToken")
        token = None
        if token_el is not None and (token_el.text or "").strip():
            token = token_el.text.strip()
        return records, token

    @staticmethod
    def _parse_record(record: ET.Element) -> dict | None:
        header = record.find(f"{OAI_NS}header")
        if header is not None and header.get("status") == "deleted":
            return None
        identifier = header.findtext(f"{OAI_NS}identifier", default="") if header is not None else ""
        paper_id = identifier.replace(ID_PREFIX, "").strip()
        if not paper_id or "/" not in paper_id:
            return None

        dc = record.find(f".//{{{NS['oai_dc']}}}dc")
        if dc is None:
            return None

        def all_of(tag: str) -> list[str]:
            return [(el.text or "").strip() for el in dc.findall(f"{DC_NS}{tag}") if (el.text or "").strip()]

        dates = sorted(all_of("date"))
        title = " ".join(all_of("title")).strip()
        description = "\n\n".join(all_of("description")).strip() or None
        rights = (all_of("rights") or [None])[0]
        return {
            "paper_id": paper_id,
            "title": title or None,
            "authors": all_of("creator"),
            "categories": all_of("subject"),
            "abstract": description,
            "rights": rights,
            "published": dates[0] if dates else None,
            "updated": dates[-1] if dates else None,
            "url": f"{BASE_URL}/{paper_id}",
            "pdf_url": f"{BASE_URL}/{paper_id}.pdf",
            "meta_source": "oai",
        }

    def harvest(
        self,
        *,
        date_from: str | None = None,
        date_until: str | None = None,
        resumption_token: str | None = None,
        on_batch: Callable[[list[dict], str | None], None] | None = None,
        progress: Callable[[int, str], None] | None = None,
    ) -> Iterator[list[dict]]:
        token = resumption_token
        total = 0
        while True:
            records, token = self.list_records(
                date_from=date_from, date_until=date_until, resumption_token=token
            )
            if records:
                total += len(records)
                if progress:
                    progress(total, token or "")
                yield records
            if on_batch:
                on_batch(records, token)
            if not token:
                break
