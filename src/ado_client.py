"""Azure DevOps REST: WIQL queries, JSON Patch, comments, marker-block edits."""

import sys
from pathlib import Path
from typing import Iterable

import requests

BASE = "https://dev.azure.com"
API_VERSION = "7.1-preview.4"
NON_JSON_BODY_DUMP = Path("/tmp/ado-non-json-response.html")


def _make_auth(pat: str | None = "") -> dict[str, str]:
    if not pat:
        raise RuntimeError("No ADO credentials: set ADO_PAT")
    return {"Authorization": f"Bearer {pat}"}


# Description / AC: additive + idempotent-on-rerun via marker block.
BEGIN = "<!-- ticket-refinery:begin -->"
END = "<!-- ticket-refinery:end -->"


def _replace_block(html: str, payload: str) -> str:
    block = f"{BEGIN}\n{payload}\n{END}"
    if BEGIN in html and END in html and html.index(BEGIN) < html.index(END):
        i = html.index(BEGIN)
        j = html.index(END) + len(END)
        return html[:i] + block + html[j:]
    return f"{html.rstrip()}\n\n{block}" if html.strip() else block


class AdoClient:
    def __init__(self, org: str, project: str, pat: str | None = None):
        self.org = org
        self.project = project
        self.pat = pat or ""
        self._auth = _make_auth(pat)

    def _url(self, path: str) -> str:
        return f"{BASE}/{self.org}/{self.project}{path}"

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = dict(self._auth)
        if extra:
            headers.update(extra)
        return headers

    def _patch(self, item_id: int, field: str, value) -> None:
        url = f"{self._url('/_apis/wit/workitems')}/{item_id}?api-version=7.1"
        body = [{"op": "add", "path": f"/fields/{field}", "value": value}]
        r = requests.patch(
            url,
            json=body,
            headers=self._headers({"Content-Type": "application/json-patch+json"}),
        )
        r.raise_for_status()

    def query_items(self, tag: str, exclude_tags: Iterable[str]) -> list[dict]:
        """WIQL: items with <tag>, excluding any of <exclude_tags>."""
        not_clauses = " AND ".join(
            f"[System.Tags] NOT CONTAINS '{t}'" for t in exclude_tags
        )
        wiql = (
            f"SELECT [System.Id] FROM WorkItems "
            f"WHERE [System.Tags] CONTAINS '{tag}' AND {not_clauses}"
        )
        url = f"{self._url('/_apis/wit/wiql')}?api-version=7.1"
        print(f"[ado_client] POST {url}", file=sys.stderr)
        print(
            f"[ado_client] org={self.org!r} project={self.project!r} tag={tag!r}",
            file=sys.stderr,
        )
        r = requests.post(url, json={"query": wiql}, headers=self._headers())
        print(
            f"[ado_client] <- {r.status_code} {r.reason} "
            f"content-type={r.headers.get('content-type')!r} "
            f"len={len(r.text)}",
            file=sys.stderr,
        )
        if r.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f"WIQL POST failed: HTTP {r.status_code} {r.reason} from {url}",
                response=r,
            )
        # ponytail: ADO sometimes returns HTML (auth challenge, proxy error,
        # wrong-org redirect) with a 2xx status. Dump the body to a file and
        # print the URL + status so the failure mode is obvious instead of a
        # JSON parse error.
        if "json" not in r.headers.get("content-type", "").lower():
            NON_JSON_BODY_DUMP.write_text(r.text, encoding="utf-8", errors="replace")
            print(
                f"[ado_client] non-JSON response.\n"
                f"  status={r.status_code}\n"
                f"  content-type={r.headers.get('content-type')!r}\n"
                f"  location={r.headers.get('Location') or r.headers.get('location')!r}\n"
                f"  body_len={len(r.text)}\n"
                f"  body_dump={NON_JSON_BODY_DUMP}",
                file=sys.stderr,
            )
            print("--- body begin ---", file=sys.stderr)
            print(r.text, file=sys.stderr, end="" if r.text.endswith("\n") else "\n")
            print("--- body end ---", file=sys.stderr)
            raise requests.exceptions.HTTPError(
                f"WIQL returned non-JSON content-type "
                f"'{r.headers.get('content-type')}' status {r.status_code} from {url}: "
                f"body_dump={NON_JSON_BODY_DUMP}",
                response=r,
            )
        ids = [w["id"] for w in r.json().get("workItems", [])]
        if not ids:
            return []
        items: list[dict] = []
        # ponytail: fixed batch size 200 = ADO REST limit. Wrap in a loop only if
        # paging becomes a real cost (today's queue is single-digit).
        for i in range(0, len(ids), 200):
            batch = ids[i : i + 200]
            u = (
                f"{self._url('/_apis/wit/workitems')}"
                f"?ids={','.join(map(str, batch))}&api-version=7.1"
            )
            rr = requests.get(u, headers=self._headers())
            rr.raise_for_status()
            items.extend(rr.json()["value"])
        return items

    def comment(self, item_id: int, body: str) -> None:
        r = requests.post(
            f"{self._url(f'/_apis/wit/workItems/{item_id}/comments')}?format=markdown&api-version={API_VERSION}",
            json={"text": body},
            headers=self._headers(),
        )
        r.raise_for_status()

    def get_comments(self, item_id: int, top: int | None = None) -> list[dict]:
        url = f"{self._url(f'/_apis/wit/workItems/{item_id}/comments')}?api-version={API_VERSION}"
        if top is not None:
            url = f"{url}&$top={top}"
        r = requests.get(url, headers=self._headers())
        r.raise_for_status()
        return r.json().get("comments", [])

    def upload_attachment(self, filename: str, content: bytes) -> dict:
        # ponytail: attachments endpoint has no -preview.4 — use stable 7.1
        url = (
            f"{self._url('/_apis/wit/attachments')}?fileName={filename}&api-version=7.1"
        )
        r = requests.post(
            url,
            data=content,
            headers=self._headers({"Content-Type": "application/octet-stream"}),
        )
        r.raise_for_status()
        return r.json()

    def add_attachment_relation(self, item_id: int, attachment_url: str) -> None:
        url = f"{self._url('/_apis/wit/workitems')}/{item_id}?api-version=7.1"
        body = [
            {
                "op": "add",
                "path": "/relations/-",
                "value": {"rel": "AttachedFile", "url": attachment_url},
            }
        ]
        r = requests.patch(
            url,
            json=body,
            headers=self._headers({"Content-Type": "application/json-patch+json"}),
        )
        r.raise_for_status()

    def add_tag(self, item: dict, tag: str) -> None:
        current = item["fields"].get("System.Tags", "") or ""
        parts = [t for t in current.split("; ") if t]
        if tag in parts:
            return
        parts.append(tag)
        self._patch(item["id"], "System.Tags", "; ".join(parts))

    def remove_tag(self, item: dict, tag: str) -> None:
        current = item["fields"].get("System.Tags", "") or ""
        parts = [t for t in current.split("; ") if t and t != tag]
        self._patch(item["id"], "System.Tags", "; ".join(parts))

    def patch_description(self, item: dict, html_payload: str) -> None:
        current = item["fields"].get("System.Description", "") or ""
        self._patch(
            item["id"], "System.Description", _replace_block(current, html_payload)
        )

    def patch_acceptance_criteria(self, item: dict, html_payload: str) -> None:
        current = (
            item["fields"].get("Microsoft.VSTS.Common.AcceptanceCriteria", "") or ""
        )
        self._patch(
            item["id"],
            "Microsoft.VSTS.Common.AcceptanceCriteria",
            _replace_block(current, html_payload),
        )

    def patch_title(self, item_id: int, title: str) -> None:
        # Caller must check ALLOW_TITLE_EDITS before invoking.
        self._patch(item_id, "System.Title", title)

    def set_field(self, item_id: int, field: str, value) -> None:
        self._patch(item_id, field, value)
