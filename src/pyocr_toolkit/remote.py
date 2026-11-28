"""Import documents from S3 (``s3://bucket/key``) and web links (``https://...``).

On a laptop, documents sit on the local disk, but on an EC2 instance or in
SageMaker they usually live in S3. This module turns remote locations into
``(filename, bytes)`` pairs for :func:`workspaces.ingest`, the function local
uploads already go through. So a remote document becomes an ordinary local copy
in ``data/documents/<id>/``: nothing else (caches, page numbers, source ids)
changes, and an imported document keeps working if the bucket is later deleted.

No environment detection is needed. boto3 looks for credentials in a fixed
order (environment variables, ``~/.aws`` profiles and SSO, container
credentials, then the EC2 instance metadata service). That one chain covers a
laptop profile, an EC2 instance role and a SageMaker execution role, so the
same command works everywhere. The optional ``AWS_*`` settings, shared with the
Textract engine, override it.

Web links need no AWS credentials at all, so an S3 *presigned* URL someone sends
you works on any machine. Links to cloud metadata addresses (169.254.169.254
and friends) are refused, redirects included: on a cloud machine they serve the
machine's own credentials, which must never end up stored as a "document".

A bad location never stops the batch. It goes into ``skipped`` with a reason,
just like a broken file in a zip. Only a malformed address raises, before
anything is fetched.
"""

from __future__ import annotations

import ipaddress
import mimetypes
import os
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from email.message import Message
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from .config import Settings
from .documents import SUPPORTED_EXTENSIONS, DocumentError, DocumentStore
from .workspaces import ARCHIVE_EXTENSIONS, IngestResult, ingest

S3_INSTALL_HINT = 'pip install "pyocr-toolkit[s3]"'
#: File types worth fetching from an S3 folder; zips are unpacked by ingest().
WANTED_EXTENSIONS = SUPPORTED_EXTENSIONS | ARCHIVE_EXTENSIONS
#: Most locations (lines) accepted in one import.
MAX_LOCATIONS = 100
HTTP_TIMEOUT = httpx.Timeout(30.0, read=120.0)

_SCHEMES = ("s3", "http", "https")
# 3-63 characters for buckets; access point aliases and S3-compatible servers can be longer
_BUCKET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,254}$")
_METADATA_HOSTS = {"metadata", "metadata.google.internal", "instance-data", "instance-data.ec2.internal"}
_EC2_IPV6_METADATA = ipaddress.ip_address("fd00:ec2::254")

#: botocore's ``Credentials.method`` -> where that is, in words a user recognizes.
CREDENTIAL_SOURCES = {
    "explicit": "the AWS_ACCESS_KEY_ID setting (.env or Providers page)",
    "env": "environment variables (AWS_ACCESS_KEY_ID)",
    "shared-credentials-file": "~/.aws/credentials",
    "config-file": "~/.aws/config",
    "sso": "AWS SSO (~/.aws/config)",
    "login": "aws login (~/.aws/config)",
    "custom-process": "credential_process (~/.aws/config)",
    "assume-role": "an assumed role (~/.aws/config)",
    "assume-role-with-web-identity": "a web identity token (EKS / IRSA)",
    "container-role": "the container role (ECS, SageMaker Studio)",
    "iam-role": "the instance role (EC2 / SageMaker instance metadata)",
    "boto-config": "a boto config file",
    "ec2-credentials-file": "an EC2 credentials file",
}
_CREDENTIAL_ERRORS = {"NoCredentialsError", "PartialCredentialsError", "CredentialRetrievalError",
                      "NoAuthTokenError", "SSOTokenLoadError", "UnauthorizedSSOTokenError"}
_REJECTED_CREDENTIALS = {"InvalidAccessKeyId", "SignatureDoesNotMatch", "ExpiredToken", "InvalidToken",
                         "TokenRefreshRequired"}


@dataclass(frozen=True)
class Location:
    """One parsed address: ``s3`` with a bucket and key (empty or ``/``-ended = a folder), or ``http(s)``."""

    raw: str
    scheme: str
    bucket: str = ""
    key: str = ""

    @property
    def is_folder(self) -> bool:
        return self.scheme == "s3" and (not self.key or self.key.endswith("/"))

    @property
    def name(self) -> str:
        """Last path part, for workspace name suggestions: ``s3://b/invoices-2024/`` -> ``invoices-2024``."""
        if self.scheme == "s3":
            return PurePosixPath(self.key).name or self.bucket
        return unquote(PurePosixPath(urlsplit(self.raw).path).name) or (urlsplit(self.raw).hostname or "")


def is_remote(text: str) -> bool:
    """Whether ``text`` looks like an address this module handles (``s3://``, ``http://``, ``https://``)."""
    scheme, sep, _ = text.strip().partition("://")
    return bool(sep) and scheme.lower() in _SCHEMES


def check_host(host: str | None) -> None:
    """Refuse cloud metadata endpoints: they hand out this machine's credentials."""
    h = (host or "").lower().rstrip(".").strip("[]")
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        ip = None
    if h in _METADATA_HOSTS or (ip is not None and (ip.is_link_local or ip == _EC2_IPV6_METADATA)):
        raise DocumentError(f"refusing to fetch from {host}: it's a cloud metadata address, which serves this "
                            "machine's credentials, not documents")


def parse_location(text: str) -> Location:
    raw = text.strip()
    scheme, sep, rest = raw.partition("://")
    scheme = scheme.lower()
    if not sep or scheme not in _SCHEMES:
        raise DocumentError(f"'{raw}' isn't an s3:// or https:// address. "
                            "Write s3://bucket/file.pdf, s3://bucket/folder/ or an https:// link.")
    if scheme == "s3":
        # split by hand: keys may contain '?' and '#', which urlsplit would cut off
        bucket, _, key = rest.partition("/")
        if not _BUCKET_RE.match(bucket):
            raise DocumentError(f"'{raw}' has no valid bucket name. Write s3://bucket/file.pdf or s3://bucket/folder/.")
        return Location(raw, "s3", bucket, key)
    parts = urlsplit(raw)
    if not parts.hostname:
        raise DocumentError(f"'{raw}' has no host name")
    check_host(parts.hostname)
    return Location(raw, scheme)


def parse_locations(texts: Iterable[str]) -> list[Location]:
    """Parse every non-empty line (duplicates dropped). Raises on the first bad one, so nothing is half-imported."""
    seen: dict[str, Location] = {}
    for text in texts:
        if text.strip() and text.strip() not in seen:
            seen[text.strip()] = parse_location(text)
    if not seen:
        raise DocumentError("nothing to import: give at least one s3:// or https:// address")
    if len(seen) > MAX_LOCATIONS:
        raise DocumentError(f"too many addresses ({len(seen)}); import at most {MAX_LOCATIONS} at a time, "
                            "or point at an S3 folder instead")
    return list(seen.values())


def _boto_kwargs(settings: Settings) -> dict[str, Any]:
    """Keys and region to hand boto3: only values set in .env or on the Providers page.

    Values that merely mirror the process environment are left out, so boto3 reads them
    itself and ``pyocr doctor`` can say they came from environment variables.
    """
    out: dict[str, Any] = {}
    for name in ("aws_region", "aws_access_key_id", "aws_secret_access_key", "aws_session_token"):
        value = getattr(settings, name) or None
        if value is not None and value != os.environ.get(name.upper()):
            out["region_name" if name == "aws_region" else name] = value
    return out


def make_s3_client(settings: Settings) -> Any:
    """A boto3 S3 client using the AWS_* settings when set, otherwise the standard credential chain."""
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:
        raise DocumentError(f"importing from s3:// needs boto3: {S3_INSTALL_HINT}. "
                            "(https:// links, including S3 presigned URLs, work without it.)") from exc
    return boto3.client("s3", **_boto_kwargs(settings), config=Config(retries={"mode": "standard", "max_attempts": 4}))


def make_http_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """An httpx client that follows redirects but refuses metadata addresses on every hop."""
    return httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True, transport=transport,
                        event_hooks={"request": [lambda r: check_host(r.url.host)]})


def aws_status(settings: Settings) -> tuple[bool, str]:
    """For ``pyocr doctor``: which credentials S3 would use and where they come from. Never their values.

    Off AWS this can take a second or two: boto3 asks the instance metadata service before giving up.
    """
    try:
        import boto3
    except ImportError:
        return False, f"boto3 not installed ({S3_INSTALL_HINT}); https:// links still work"
    session = boto3.Session(**_boto_kwargs(settings))
    try:
        creds = session.get_credentials()
    except Exception as exc:  # a broken profile, an expired SSO login, ...
        return False, f"AWS credentials could not be loaded: {exc}"
    if creds is None:
        return False, ("no AWS credentials found: run `aws configure`, set AWS_PROFILE, "
                       "or attach an IAM role to the instance")
    region = session.region_name or "not set (fine for S3)"
    return True, f"credentials from {CREDENTIAL_SOURCES.get(creds.method, creds.method)}, region {region}"


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code", ""))
    return ""


def _mb(n: int) -> int:
    return n // (1024 * 1024)


class RemoteFetcher:
    """Yields ``(filename, bytes)`` for each document behind a list of locations, one download at a time.

    Problems are appended to ``result.skipped`` (``{"name", "reason"}``), the same list ingest() uses.
    Clients can be passed in, which is how tests run without boto3 or a network.
    """

    def __init__(self, settings: Settings, result: IngestResult, *, s3_client: Any = None,
                 http_client: httpx.Client | None = None, progress: Callable[[str], None] | None = None) -> None:
        self.settings = settings
        self.result = result
        self.max_bytes = settings.max_upload_mb * 1024 * 1024
        self.budget = settings.import_max_files
        self.progress = progress or (lambda _label: None)
        self._s3 = s3_client
        self._http = http_client
        self._s3_broken: str | None = None  # set once credentials fail: no point asking again

    def skip(self, name: str, reason: str) -> None:
        self.result.skipped.append({"name": name, "reason": reason})

    def files(self, locations: Iterable[Location]) -> Iterator[tuple[str, bytes]]:
        """Set up clients now (so a missing boto3 fails before anything is imported), then fetch lazily."""
        locations = list(locations)
        if any(loc.scheme == "s3" for loc in locations) and self._s3 is None:
            self._s3 = make_s3_client(self.settings)
        own_http = None
        if self._http is None and any(loc.scheme != "s3" for loc in locations):
            own_http = self._http = make_http_client()
        return self._fetch_all(locations, own_http)

    def _fetch_all(self, locations: list[Location], own_http: httpx.Client | None) -> Iterator[tuple[str, bytes]]:
        try:
            for loc in locations:
                yield from (self._from_s3(loc) if loc.scheme == "s3" else self._from_http(loc))
        finally:
            if own_http is not None:
                own_http.close()

    # ------------------------------------------------------------ S3
    def _s3_failed(self, label: str, exc: Exception) -> None:
        name, code = type(exc).__name__, _error_code(exc)
        if name in _CREDENTIAL_ERRORS:
            self._s3_broken = (f"no usable AWS credentials ({exc}). Run `aws configure`, set AWS_PROFILE, "
                               "attach an IAM role, or set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY")
            reason = self._s3_broken
        elif code in _REJECTED_CREDENTIALS:
            self._s3_broken = f"AWS rejected the credentials ({code}); refresh them or check AWS_ACCESS_KEY_ID"
            reason = self._s3_broken
        elif code in ("AccessDenied", "403", "AllAccessDisabled"):
            reason = ("access denied: the AWS identity in use needs s3:GetObject on the files "
                      "(and s3:ListBucket on the bucket to import a folder)")
        elif code in ("NoSuchBucket",):
            reason = "no such bucket"
        elif code in ("NoSuchKey", "404"):
            reason = "no such file"
        else:
            reason = str(exc) or name
        self.skip(label, reason)

    def _from_s3(self, loc: Location) -> Iterator[tuple[str, bytes]]:
        if self._s3_broken:
            self.skip(loc.raw, self._s3_broken)
            return
        if loc.is_folder:
            yield from self._s3_folder(loc.bucket, loc.key, loc.raw)
            return
        label = loc.raw
        try:
            obj = self._s3.get_object(Bucket=loc.bucket, Key=loc.key)
        except Exception as exc:
            if _error_code(exc) in ("NoSuchKey", "404"):
                # "s3://bucket/scans" usually means the folder scans/
                yield from self._s3_folder(loc.bucket, loc.key + "/", label, missing_ok=False)
                return
            self._s3_failed(label, exc)
            return
        if (data := self._s3_body(label, loc.key, obj)) is not None:
            yield self._s3_name(loc.key, obj), data

    def _s3_folder(self, bucket: str, prefix: str, label: str, missing_ok: bool = True) -> Iterator[tuple[str, bytes]]:
        keys = self._s3_list(bucket, prefix, label)
        if keys is None:
            return
        if not keys:
            self.skip(label, f"no supported files under {prefix or 'the bucket root'}" if missing_ok else "no such file")
            return
        for key, size in keys:
            item = f"s3://{bucket}/{key}"
            if size > self.max_bytes:
                self.skip(item, f"larger than {_mb(self.max_bytes)} MB")
                continue
            try:
                obj = self._s3.get_object(Bucket=bucket, Key=key)
            except Exception as exc:
                self._s3_failed(item, exc)
                if self._s3_broken:
                    return
                continue
            if (data := self._s3_body(item, key, obj)) is not None:
                yield self._s3_name(key, obj), data

    def _s3_list(self, bucket: str, prefix: str, label: str) -> list[tuple[str, int]] | None:
        """Supported, non-hidden keys under ``prefix`` with their sizes, or None if listing failed."""
        out: list[tuple[str, int]] = []
        token = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            try:
                page = self._s3.list_objects_v2(**kwargs)
            except Exception as exc:
                self._s3_failed(label, exc)
                return None
            for obj in page.get("Contents", []):
                key = obj["Key"]
                rel = key[len(prefix):]
                if not rel or key.endswith("/") or any(part.startswith(".") for part in rel.split("/")):
                    continue  # folder markers, dotfiles and hidden folders
                if PurePosixPath(key).suffix.lower() not in WANTED_EXTENSIONS:
                    continue
                out.append((key, int(obj.get("Size", 0))))
            if len(out) > self.budget:
                self.skip(label, f"more than {self.budget} files to import; point at a smaller folder "
                                 "or raise IMPORT_MAX_FILES")
                return None
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
        self.budget -= len(out)
        return out

    def _s3_body(self, label: str, key: str, obj: dict) -> bytes | None:
        body = obj["Body"]
        try:
            ext = PurePosixPath(key).suffix.lower()
            if ext and ext not in WANTED_EXTENSIONS:
                self.skip(label, f"unsupported file type '{ext}'")
                return None
            if int(obj.get("ContentLength") or 0) > self.max_bytes:
                self.skip(label, f"larger than {_mb(self.max_bytes)} MB")
                return None
            self.progress(label)
            data = body.read(self.max_bytes + 1)
        except Exception as exc:  # the connection dropped mid-download
            self.skip(label, f"download failed: {exc}")
            return None
        finally:
            body.close()
        if len(data) > self.max_bytes:
            self.skip(label, f"larger than {_mb(self.max_bytes)} MB")
            return None
        return data

    @staticmethod
    def _s3_name(key: str, obj: dict) -> str:
        name = PurePosixPath(key).name or "download"
        return name if PurePosixPath(name).suffix else name + _ext_for_type(obj.get("ContentType"))

    # ------------------------------------------------------------ http(s)
    def _from_http(self, loc: Location) -> Iterator[tuple[str, bytes]]:
        label = _redact(loc.raw)
        try:
            self.progress(label)
            with self._http.stream("GET", loc.raw) as resp:
                if resp.status_code >= 400:
                    hint = " (a presigned link may have expired)" if resp.status_code in (400, 403) else ""
                    self.skip(label, f"HTTP {resp.status_code}{hint}")
                    return
                if int(resp.headers.get("content-length") or 0) > self.max_bytes:
                    self.skip(label, f"larger than {_mb(self.max_bytes)} MB")
                    return
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf += chunk
                    if len(buf) > self.max_bytes:
                        self.skip(label, f"larger than {_mb(self.max_bytes)} MB")
                        return
                name = _http_name(loc, resp)
        except DocumentError as exc:  # a redirect to a metadata address
            self.skip(label, str(exc))
            return
        except httpx.HTTPError as exc:
            self.skip(label, f"download failed: {exc}")
            return
        yield name, bytes(buf)


def _ext_for_type(content_type: str | None) -> str:
    ext = mimetypes.guess_extension((content_type or "").split(";")[0].strip()) or ""
    return ext if ext in WANTED_EXTENSIONS else ""


def _http_name(loc: Location, resp: httpx.Response) -> str:
    """Content-Disposition filename, else the URL's last path part, else ``download``; plus an extension if missing."""
    msg = Message()
    if cd := resp.headers.get("content-disposition"):
        msg["content-disposition"] = cd
    name = msg.get_filename() or loc.name or "download"
    name = PurePosixPath(name.replace("\\", "/")).name or "download"
    return name if PurePosixPath(name).suffix else name + _ext_for_type(resp.headers.get("content-type"))


def _redact(url: str) -> str:
    """Drop the query string from labels: presigned URLs carry a signature there, which shouldn't be echoed."""
    parts = urlsplit(url)
    return parts._replace(query="", fragment="").geturl() if parts.query else url


def import_remote(store: DocumentStore, texts: Iterable[str], settings: Settings, *, s3_client: Any = None,
                  http_client: httpx.Client | None = None, progress: Callable[[str], None] | None = None) -> IngestResult:
    """Fetch every location and add what it holds to ``store`` (zips unpacked). Shared by the API and the CLI."""
    locations = parse_locations(texts)
    result = IngestResult()
    fetcher = RemoteFetcher(settings, result, s3_client=s3_client, http_client=http_client, progress=progress)
    return ingest(store, fetcher.files(locations), max_bytes=fetcher.max_bytes, result=result)
