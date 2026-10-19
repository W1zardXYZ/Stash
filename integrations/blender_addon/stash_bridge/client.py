"""Tiny HTTP client for the Stash REST API (stdlib only, usable outside Blender too)."""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class StashError(RuntimeError):
    pass


class StashClient:
    def __init__(self, base_url: str, token: str = "", timeout: float = 60):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    # ---------------------------------------------------------------- basics
    def _req(self, method, path, body=None, headers=None, raw=False):
        url = path if path.startswith("http") else f"{self.base}{path}"
        h = {"Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None and not isinstance(body, (bytes, bytearray)):
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        elif body is not None:
            data = bytes(body)
        h.update(headers or {})
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = r.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except Exception:
                pass
            raise StashError(f"{e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise StashError(f"cannot reach Stash at {self.base}: {e.reason}") from None
        return payload if raw else json.loads(payload or b"null")

    def get(self, path, **params):
        q = {k: v for k, v in params.items() if v not in (None, "", [])}
        return self._req("GET", path + ("?" + urllib.parse.urlencode(q) if q else ""))

    def post(self, path, body=None):
        return self._req("POST", path, body if body is not None else {})

    # ---------------------------------------------------------------- API
    def status(self):
        return self.get("/api/status")

    def search(self, q="", kind="", page=1, per_page=40, **extra):
        return self.get("/api/assets", q=q, kind=kind, page=page, per_page=per_page, sort="relevance" if q else "updated", **extra)

    def asset(self, asset_id):
        return self.get(f"/api/assets/{asset_id}")

    def find_by_uid(self, uid):
        r = self.search(q=uid, per_page=5)
        for it in r.get("items", []):
            if it["uid"] == uid:
                return it
        return None

    def download(self, url_path, dest_path):
        data = self._req("GET", url_path, raw=True)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(data)
        return dest_path

    def export(self, asset_id, target, dest_dir, poll=1.0, timeout=1800, progress=None):
        """Ask the server for `target`, wait for the conversion, download the file. Returns the local path."""
        r = self.post(f"/api/assets/{asset_id}/export", {"target": target})
        if "file" in r:
            f = r["file"]
            return self.download(f["url"], os.path.join(dest_dir, f["name"]))
        job_id = r["job_id"]
        t0 = time.time()
        while True:
            j = self.get(f"/api/jobs/{job_id}")
            if progress:
                progress(j.get("progress", 0), j.get("message", ""))
            if j["status"] == "done":
                res = j["result"]
                return self.download(f"/api/files/{res['file_id']}/{urllib.parse.quote(res['name'])}", os.path.join(dest_dir, res["name"]))
            if j["status"] in ("error", "cancelled"):
                raise StashError(f"export failed: {j.get('message')}")
            if time.time() - t0 > timeout:
                raise StashError("export timed out")
            time.sleep(poll)

    def upload_bundle(self, zip_path):
        return self._multipart("/api/ingest/bundle", [("file", os.path.basename(zip_path), zip_path)])

    def upload_files(self, paths):
        fields = [("files", os.path.basename(p), p) for p in paths]
        extra = [("paths", os.path.basename(p)) for p in paths]
        return self._multipart("/api/upload", fields, extra)

    def _multipart(self, path, files, fields=()):
        boundary = uuid.uuid4().hex
        parts = []
        for name, value in fields:
            parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
        for name, filename, fpath in files:
            with open(fpath, "rb") as f:
                content = f.read()
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{filename}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n".encode() + content + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode())
        return self._req("POST", path, b"".join(parts), headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
