"""Import from s3:// and web links (remote.py), against a fake S3 client and httpx.MockTransport.

No boto3, AWS account or network needed: S3 is a small in-memory fake with the same
method names, arguments and error shapes (``exc.response["Error"]["Code"]``) as boto3.
"""

from __future__ import annotations

import io
import sys
import types
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from pyocr_toolkit import cli, remote
from pyocr_toolkit.api import create_app
from pyocr_toolkit.config import Settings
from pyocr_toolkit.documents import DocumentError, DocumentStore
from pyocr_toolkit.remote import (
    MAX_LOCATIONS,
    import_remote,
    is_remote,
    make_http_client,
    parse_location,
    parse_locations,
)
from pyocr_toolkit.workspaces import WorkspaceStore


# ---------------------------------------------------------------- a fake S3
class S3Error(Exception):
    """Shaped like botocore's ClientError."""

    def __init__(self, code: str):
        super().__init__(f"An error occurred ({code})")
        self.response = {"Error": {"Code": code}}


class NoCredentialsError(Exception):  # same class name as botocore's
    pass


class Body:
    def __init__(self, data: bytes):
        self.data, self.closed = data, False

    def read(self, amt=None):
        return self.data if amt is None else self.data[:amt]

    def close(self):
        self.closed = True


class FakeS3:
    def __init__(self, objects: dict[str, bytes], *, page_size: int = 1000, fail: dict | None = None,
                 types_: dict | None = None):
        self.objects = objects  # "bucket/key" -> bytes
        self.page_size = page_size
        self.fail = fail or {}  # key, "list" or "*" -> exception to raise
        self.types = types_ or {}
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[Body] = []

    def get_object(self, Bucket, Key):  # noqa: N803 - boto3's argument names
        self.calls.append(("get", Key))
        if exc := self.fail.get(Key) or self.fail.get("*"):
            raise exc
        if f"{Bucket}/{Key}" not in self.objects:
            raise S3Error("NoSuchKey")
        data = self.objects[f"{Bucket}/{Key}"]
        self.bodies.append(body := Body(data))
        return {"Body": body, "ContentLength": len(data), "ContentType": self.types.get(Key, "binary/octet-stream")}

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):  # noqa: N803
        self.calls.append(("list", Prefix))
        if exc := self.fail.get("list") or self.fail.get("*"):
            raise exc
        keys = sorted(k.split("/", 1)[1] for k in self.objects
                      if k.startswith(Bucket + "/") and k.split("/", 1)[1].startswith(Prefix))
        start = int(ContinuationToken or 0)
        more = start + self.page_size < len(keys)
        page = {"IsTruncated": more}
        if keys[start:start + self.page_size]:  # like S3, no "Contents" on an empty page
            page["Contents"] = [{"Key": k, "Size": len(self.objects[f"{Bucket}/{k}"])}
                                for k in keys[start:start + self.page_size]]
        if more:
            page["NextContinuationToken"] = str(start + self.page_size)
        return page

    def gets(self):
        return [key for op, key in self.calls if op == "get"]


def _zip(**members: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _run(tmp_path, urls, *, s3=None, http=None, **settings):
    store = DocumentStore(tmp_path / "documents")
    result = import_remote(store, urls, Settings(data_dir=tmp_path, _env_file=None, **settings),
                           s3_client=s3, http_client=http)
    return store, result


def _reasons(result) -> dict[str, str]:
    return {s["name"]: s["reason"] for s in result.skipped}


@pytest.fixture
def bucket(sample_pdf, sample_png):
    return {
        "bkt/scans/a.pdf": sample_pdf,
        "bkt/scans/sub/b.png": sample_png,
        "bkt/scans/batch.zip": _zip(**{"c.pdf": sample_pdf}),
        "bkt/scans/.hidden/x.pdf": sample_pdf,  # hidden folder
        "bkt/scans/._a.pdf": b"resource fork",  # dotfile
        "bkt/scans/notes.docx": b"not a document type",
        "bkt/scans/": b"",  # the "folder" marker the S3 console creates
        "bkt/other/z.pdf": sample_pdf,
    }


# ---------------------------------------------------------------- parsing
def test_parse_locations():
    loc = parse_location("s3://bkt/a/b.pdf")
    assert (loc.scheme, loc.bucket, loc.key, loc.is_folder, loc.name) == ("s3", "bkt", "a/b.pdf", False, "b.pdf")
    assert parse_location("S3://bkt/scans/").is_folder and parse_location("s3://bkt/invoices-2024/").name == "invoices-2024"
    assert parse_location("s3://bkt").is_folder and parse_location("s3://bkt").name == "bkt"
    assert parse_location("s3://bkt/we?ird#key.pdf").key == "we?ird#key.pdf"  # '?' and '#' are legal in keys
    assert parse_location("https://example.com/files/r%20q.pdf?sig=1").name == "r q.pdf"
    for bad in ("s3://", "s3:///key.pdf", "s3://ab/too-short.pdf", "ftp://host/a.pdf", "folder/a.pdf", "https:///nohost"):
        with pytest.raises(DocumentError):
            parse_location(bad)
    for metadata in ("http://169.254.169.254/latest/meta-data/", "http://[fd00:ec2::254]/",
                     "http://metadata.google.internal/computeMetadata/v1/", "http://169.254.170.2/v2/credentials"):
        with pytest.raises(DocumentError, match="metadata"):
            parse_location(metadata)
    assert is_remote(" s3://bkt/k ") and is_remote("HTTPS://x") and not is_remote("C:/data/a.pdf")
    assert not is_remote("scans/a.pdf")
    assert [loc.raw for loc in parse_locations(["s3://bkt/a.pdf", " ", "s3://bkt/a.pdf ", "s3://bkt/c/"])] == \
        ["s3://bkt/a.pdf", "s3://bkt/c/"]
    with pytest.raises(DocumentError, match="nothing to import"):
        parse_locations(["", "  "])
    with pytest.raises(DocumentError, match="too many"):
        parse_locations([f"s3://bkt/{i}.pdf" for i in range(MAX_LOCATIONS + 1)])


# ---------------------------------------------------------------- S3
def test_s3_folder_file_and_folder_without_slash(tmp_path, bucket):
    s3 = FakeS3(bucket, page_size=2)  # several pages of listing
    store, result = _run(tmp_path, ["s3://bkt/scans/"], s3=s3)
    assert sorted(m.filename for m in result.documents) == ["a.pdf", "b.png", "c.pdf"]  # the zip was unpacked
    assert result.skipped == []
    assert sum(op == "list" for op, _ in s3.calls) > 1
    assert sorted(s3.gets()) == ["scans/a.pdf", "scans/batch.zip", "scans/sub/b.png"]  # nothing hidden fetched
    assert all(b.closed for b in s3.bodies)

    _, one = _run(tmp_path, ["s3://bkt/other/z.pdf"], s3=FakeS3(bucket))
    assert [m.filename for m in one.documents] == ["z.pdf"]
    assert store.get(one.documents[0].id).meta.page_count == 2

    _, folder = _run(tmp_path, ["s3://bkt/scans"], s3=FakeS3(bucket))  # no trailing slash: still the folder
    assert len(folder.documents) == 3


def test_s3_problems_are_skipped_with_reasons(tmp_path, bucket, sample_pdf):
    bucket = {**bucket, "bkt/big/huge.pdf": b"%PDF" + b"0" * (2 * 1024 * 1024 + 10), "bkt/big/ok.pdf": sample_pdf,
              "bkt/locked/secret.pdf": sample_pdf, "bkt/noext": sample_pdf}
    s3 = FakeS3(bucket, fail={"locked/secret.pdf": S3Error("AccessDenied")}, types_={"noext": "application/pdf"})
    _, result = _run(tmp_path, ["s3://bkt/missing.pdf", "s3://bkt/locked/secret.pdf", "s3://bkt/scans/notes.docx",
                                "s3://bkt/big/", "s3://bkt/empty/", "s3://bkt/noext"], s3=s3, max_upload_mb=2)
    reasons = _reasons(result)
    assert reasons["s3://bkt/missing.pdf"] == "no such file"
    assert "s3:GetObject" in reasons["s3://bkt/locked/secret.pdf"]
    assert reasons["s3://bkt/scans/notes.docx"] == "unsupported file type '.docx'"
    assert reasons["s3://bkt/big/huge.pdf"] == "larger than 2 MB"
    assert "no supported files" in reasons["s3://bkt/empty/"]
    assert sorted(m.filename for m in result.documents) == ["noext.pdf", "ok.pdf"]  # extension from Content-Type
    assert "big/huge.pdf" not in s3.gets()  # too big per the listing: never downloaded
    assert all(b.closed for b in s3.bodies)


def test_s3_without_credentials_stops_asking(tmp_path, bucket):
    s3 = FakeS3(bucket, fail={"*": NoCredentialsError("Unable to locate credentials")})
    _, result = _run(tmp_path, ["s3://bkt/other/z.pdf", "s3://bkt/scans/", "s3://bkt/x.pdf"], s3=s3)
    assert result.documents == [] and len(result.skipped) == 3
    assert all("no usable AWS credentials" in s["reason"] and "aws configure" in s["reason"] for s in result.skipped)
    assert len(s3.calls) == 1  # one failure is enough to know


def test_s3_import_max_files(tmp_path, bucket):
    s3 = FakeS3(bucket)
    _, result = _run(tmp_path, ["s3://bkt/scans/"], s3=s3, import_max_files=2)
    assert result.documents == [] and "IMPORT_MAX_FILES" in _reasons(result)["s3://bkt/scans/"]
    assert s3.gets() == []


def test_s3_needs_boto3_and_says_how_to_install(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", None)  # makes `import boto3` raise ImportError
    with pytest.raises(DocumentError, match=r"pyocr-toolkit\[s3\]"):
        _run(tmp_path, ["s3://bkt/a.pdf"])
    assert DocumentStore(tmp_path / "documents").list() == []


def test_boto3_gets_only_settings_that_the_environment_doesnt_already_hold(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFROMENV")
    monkeypatch.delenv("AWS_REGION", raising=False)
    mirrored = Settings(_env_file=None, aws_access_key_id="AKIAFROMENV", aws_region="eu-west-1")
    assert remote._boto_kwargs(mirrored) == {"region_name": "eu-west-1"}  # boto3 reads the key from env itself
    own = Settings(_env_file=None, aws_access_key_id="AKIAFROMDOTENV", aws_secret_access_key="s")
    assert remote._boto_kwargs(own) == {"aws_access_key_id": "AKIAFROMDOTENV", "aws_secret_access_key": "s"}


def test_aws_status_names_the_source_never_the_values(monkeypatch):
    creds = {"value": types.SimpleNamespace(method="iam-role", access_key="AKIASECRET", secret_key="SECRET")}

    class Session:
        def __init__(self, **kwargs):
            self.region_name = "us-west-2"

        def get_credentials(self):
            return creds["value"]

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(Session=Session))
    ok, detail = remote.aws_status(Settings(_env_file=None))
    assert ok and "instance role" in detail and "us-west-2" in detail and "SECRET" not in detail
    creds["value"] = None
    ok, detail = remote.aws_status(Settings(_env_file=None))
    assert not ok and "no AWS credentials" in detail
    monkeypatch.setitem(sys.modules, "boto3", None)
    ok, detail = remote.aws_status(Settings(_env_file=None))
    assert not ok and "pyocr-toolkit[s3]" in detail


# ---------------------------------------------------------------- web links
def test_http_links(tmp_path, sample_pdf, sample_png):
    big = b"%PDF" + b"0" * (2 * 1024 * 1024 + 10)  # the sample PDF itself is ~1.8 MB

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/files/report.pdf":
            return httpx.Response(200, content=sample_pdf)
        if path == "/dl":
            return httpx.Response(200, content=sample_pdf,
                                  headers={"content-disposition": "attachment; filename*=UTF-8''r%C3%A9sum%C3%A9.pdf"})
        if path == "/render":
            return httpx.Response(200, content=sample_png, headers={"content-type": "image/png"})
        if path == "/expired.pdf":
            return httpx.Response(403, content=b"<Error>Request has expired</Error>")
        if path == "/big.pdf":
            return httpx.Response(200, content=big)
        if path == "/chunked.pdf":  # no Content-Length: the size is checked while reading
            return httpx.Response(200, content=iter([big[:1_500_000], big[1_500_000:]]))
        if path == "/sneaky":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        return httpx.Response(404)

    with make_http_client(httpx.MockTransport(handler)) as http:
        _, result = _run(tmp_path, [
            "https://example.com/files/report.pdf", "https://example.com/dl", "https://example.com/render",
            "https://example.com/expired.pdf?X-Amz-Signature=abc123", "https://example.com/big.pdf",
            "https://example.com/chunked.pdf", "https://example.com/sneaky", "https://example.com/gone.pdf",
        ], http=http, max_upload_mb=2)
    assert sorted(m.filename for m in result.documents) == ["render.png", "report.pdf", "résumé.pdf"]
    reasons = _reasons(result)
    assert reasons["https://example.com/expired.pdf"] == "HTTP 403 (a presigned link may have expired)"
    assert "abc123" not in str(result.skipped)  # signatures are never echoed back
    assert reasons["https://example.com/big.pdf"] == reasons["https://example.com/chunked.pdf"] == "larger than 2 MB"
    assert "metadata" in reasons["https://example.com/sneaky"]  # refused even behind a redirect
    assert reasons["https://example.com/gone.pdf"] == "HTTP 404"


# ---------------------------------------------------------------- API, web and CLI
@pytest.fixture
def client(tmp_path, monkeypatch, bucket):
    s3 = FakeS3(bucket)
    monkeypatch.setattr(remote, "make_s3_client", lambda settings: s3)
    app = create_app(Settings(data_dir=tmp_path, _env_file=None))
    with TestClient(app) as c:
        c.s3 = s3
        yield c


def test_api_import_into_a_new_or_existing_workspace(client):
    r = client.post("/api/imports", json={"urls": ["s3://bkt/scans/"], "workspace_name": "Scans"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert len(body["documents"]) == 3 and body["skipped"] == []
    assert body["workspace"]["name"] == "Scans" and len(body["workspace"]["doc_ids"]) == 3
    assert len(client.get("/api/documents").json()) == 3

    ws_id = body["workspace"]["id"]
    r = client.post("/api/imports", json={"urls": ["s3://bkt/other/z.pdf"], "workspace_id": ws_id})
    assert r.status_code == 201 and len(r.json()["workspace"]["doc_ids"]) == 4

    before = len(client.s3.calls)  # the name is taken: refused before anything is fetched
    r = client.post("/api/imports", json={"urls": ["s3://bkt/other/z.pdf"], "workspace_name": "scans"})
    assert r.status_code == 400 and "already exists" in r.json()["detail"]
    assert len(client.s3.calls) == before


def test_api_import_errors(client):
    r = client.post("/api/imports", json={"urls": ["s3://bkt/missing.pdf"], "workspace_name": "Nothing"})
    assert r.status_code == 201 and r.json()["documents"] == [] and "workspace" not in r.json()
    assert r.json()["skipped"] == [{"name": "s3://bkt/missing.pdf", "reason": "no such file"}]
    assert client.get("/api/workspaces").json() == []  # no empty workspace left behind

    r = client.post("/api/imports", json={"urls": ["ftp://host/a.pdf"]})
    assert r.status_code == 400 and "s3://" in r.json()["detail"]
    assert client.post("/api/imports", json={"urls": []}).status_code == 422
    r = client.post("/api/imports", json={"urls": ["s3://bkt/x.pdf"], "workspace_id": "f" * 32})
    assert r.status_code == 404
    before = len(client.s3.calls)
    r = client.post("/api/imports", json={"urls": ["s3://bkt/other/z.pdf"]}, headers={"origin": "http://evil.example"})
    assert r.status_code == 403  # another site can't make this machine fetch with its AWS identity
    assert len(client.s3.calls) == before


def test_import_dialog_is_on_every_page(client):
    assert 'id="import-dialog"' in client.get("/").text
    ws = client.post("/api/workspaces", json={"name": "W"}).json()
    assert 'class="btn ghost import-open" data-workspace="' + ws["id"] in client.get(f"/workspaces/{ws['id']}").text


def test_cli_import_mixes_local_files_and_s3(tmp_path, monkeypatch, capsys, bucket, sample_pdf):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    cli.get_settings.cache_clear()
    monkeypatch.setattr(remote, "make_s3_client", lambda settings: FakeS3(bucket))
    (tmp_path / "local.pdf").write_bytes(sample_pdf)
    try:
        with pytest.raises(SystemExit) as e:
            cli.main(["import", "local.pdf", "s3://bkt/scans/", "-w", "mixed"])
        assert e.value.code == 0
        out = capsys.readouterr()
        assert out.out.count("✓") == 4 and "↓ s3://bkt/scans/a.pdf" in out.err
        settings = cli.get_settings()
        spaces = WorkspaceStore(settings.workspaces_dir, DocumentStore(settings.documents_dir))
        assert [(w.name, len(w.doc_ids)) for w in spaces.list()] == [("mixed", 4)]

        with pytest.raises(SystemExit) as e:  # nothing imported: an error, and no empty workspace
            cli.main(["import", "s3://bkt/missing.pdf", "s3://bkt/gone/"])
        assert e.value.code == 1 and "no such file" in capsys.readouterr().out
        assert len(spaces.list()) == 1
    finally:
        cli.get_settings.cache_clear()
