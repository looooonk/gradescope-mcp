import asyncio
import json
import os
import re
import sys
from pathlib import PurePosixPath
from urllib.parse import parse_qsl, unquote, urlsplit

import httpx
import psutil

from gradescope_mcp.catalog import GradescopeError, validate
from gradescope_mcp.config import tls_context
from gradescope_mcp.content import MAX_FILE_BYTES, text_slice

STORAGE_HOSTS = {
    "production-gradescope-uploads.s3-us-west-2.amazonaws.com",
    "production-gradescope-uploads.s3.us-west-2.amazonaws.com",
    "production-gradescope-uploads.s3.amazonaws.com",
}
SIGNED_QUERY = {
    "X-Amz-Algorithm",
    "X-Amz-Credential",
    "X-Amz-Date",
    "X-Amz-Expires",
    "X-Amz-Security-Token",
    "X-Amz-Signature",
    "X-Amz-SignedHeaders",
    "AWSAccessKeyId",
    "Expires",
    "Signature",
    "response-content-disposition",
    "response-content-type",
}


def safe_storage_url(url):
    if not isinstance(url, str):
        return False
    try:
        p = urlsplit(url)
        decoded = unquote(unquote(p.path))
        return (
            p.scheme == "https"
            and p.netloc in STORAGE_HOSTS
            and not p.fragment
            and re.fullmatch(
                r"/uploads/(pdf_attachment|image_attachment|page|text_file)/file/[1-9][0-9]*/[^/]+",
                decoded,
            )
            is not None
            and not any(c in decoded for c in "\\\r\n\x00")
            and not any(part in {".", ".."} for part in decoded.split("/"))
            and all(k in SIGNED_QUERY for k, _ in parse_qsl(p.query))
        )
    except ValueError:
        return False


def attachment_items(data):
    result = []
    seen = set()

    def add(url, name, kind, **metadata):
        if not safe_storage_url(url):
            return
        identity = urlsplit(url).path
        if identity not in seen:
            seen.add(identity)
            result.append(
                {"index": len(result), "name": name, "kind": kind, "url": url, **metadata}
            )

    pdf = data.get("pdf_attachment") or {}
    add(pdf.get("url"), pdf.get("filename") or "submission.pdf", "pdf")
    for page in pdf.get("pages", []):
        add(
            page.get("url"),
            f"Page {page.get('number')}",
            "page_image",
            page_number=page.get("number"),
        )
    for i, image in enumerate(data.get("image_attachments", [])):
        add(image.get("url"), image.get("filename") or f"Image {i + 1}", "image")
    for file in data.get("text_files", []):
        if not isinstance(file, dict):
            continue
        add(
            file.get("url") or (file.get("file") or {}).get("url"),
            file.get("path") or "source.txt",
            "source",
            size_bytes=file.get("file_size"),
        )
    return result


def public_item(item):
    return {k: v for k, v in item.items() if k != "url"}


async def list_files(client, path):
    validate("submission_files", path)
    result = await client.read("submission_files", path)
    return {
        "files": [public_item(f) for f in attachment_items(result["data"])],
        "source_url": result["source_url"],
        "untrusted_content": True,
        "note": (
            "Existing storage files only. Generated graded PDFs/ZIP exports are excluded. "
            "Use read_image for scans; feedback is available through questions."
        ),
    }


async def download_file(client, path, index, *, transport=None):
    validate("submission_files", path)
    if type(index) is not int or index < 0:
        raise GradescopeError("File index must be a nonnegative integer.")
    source = await client.read("submission_files", path)
    items = attachment_items(source["data"])
    if index >= len(items):
        raise GradescopeError("File index not found. Use gradescope_list_files first.")
    item = items[index]
    try:
        async with (
            asyncio.timeout(30),
            client.slots,
            httpx.AsyncClient(
                timeout=20,
                trust_env=False,
                verify=tls_context(),
                follow_redirects=False,
                transport=transport,
            ) as http,
            http.stream("GET", item["url"]) as response,
        ):
            if response.status_code != 200:
                raise GradescopeError(
                    "Storage file unavailable; redirects are blocked.",
                    "file_http_error",
                    response.status_code,
                )
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_FILE_BYTES:
                    raise GradescopeError("File exceeds the 25 MiB download limit.", "size_limit")
            mime = response.headers.get("content-type", "").split(";", 1)[0].lower()
    except (httpx.RequestError, TimeoutError):
        raise GradescopeError("File could not be read securely.", "connection_error") from None
    filename = item["name"]
    if item["kind"] in {"page_image", "image"}:
        filename = PurePosixPath(urlsplit(item["url"]).path).name
    return public_item(item), bytes(body), mime, filename


async def extract(body, filename, mime, *, image=False):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "gradescope_mcp.image_worker" if image else "gradescope_mcp.extract_worker",
        filename,
        mime,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={"PATH": os.defpath},
    )

    async def monitor():
        try:
            worker = psutil.Process(process.pid)
        except psutil.NoSuchProcess:
            return
        while process.returncode is None:
            try:
                if worker.memory_info().rss > 512 * 1024 * 1024:
                    process.kill()
                    return
            except psutil.NoSuchProcess:
                return
            except psutil.Error:
                process.kill()
                return
            await asyncio.sleep(0.1)

    watcher = asyncio.create_task(monitor())
    try:
        output, _ = await asyncio.wait_for(process.communicate(body), timeout=25)
    except TimeoutError:
        raise GradescopeError("Extraction exceeded its time limit.", "extraction_error") from None
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        if process.returncode is None:
            process.kill()
            await process.wait()
    if image:
        if process.returncode or not output.startswith(b"\x89PNG\r\n\x1a\n"):
            raise GradescopeError(
                "Image decoding failed or exceeded its limits.", "extraction_error"
            )
        return output
    try:
        result = json.loads(output)
        return result["text"], result["warning"]
    except (ValueError, KeyError):
        raise GradescopeError(
            "Document extraction failed or exceeded its limits.", "extraction_error"
        ) from None


async def read_file(client, path, index, offset=0, max_chars=12000, *, transport=None):
    text_slice("", offset, max_chars)
    item, body, mime, filename = await download_file(client, path, index, transport=transport)
    if mime.startswith("image/") or item["kind"] in {"page_image", "image"}:
        raise GradescopeError("Use gradescope_read_image for scanned pages or images.")
    text, warning = await extract(body, filename, mime)
    return {
        "file": item,
        "content_type": mime,
        "warning": warning,
        "untrusted_content": True,
        **text_slice(text, offset, max_chars),
    }
