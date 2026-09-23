import io
import re
import zipfile
from pathlib import PurePosixPath

from bs4 import BeautifulSoup
from defusedxml import ElementTree
from pypdf import PdfReader

from gradescope_mcp.catalog import GradescopeError

MAX_FILE_BYTES = 25 * 1024 * 1024


def html_text(value: str) -> str:
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for link in soup.find_all(["a", "link", "file", "image"]):
        href = link.get("href") or link.get("url") or link.get("src") or ""
        if href.startswith(("https://", "http://", "/")):
            link.append(f" ({href})")
    return soup.get_text("\n", strip=True)


def text_slice(text: str, offset: int, max_chars: int) -> dict:
    if offset < 0 or not 1 <= max_chars <= 60000:
        raise GradescopeError("offset must be nonnegative; max_chars must be between 1 and 60000.")
    end = min(offset + max_chars, len(text))
    return {
        "text": text[offset:end],
        "offset": offset,
        "total_chars": len(text),
        "next_offset": end if end < len(text) else None,
    }


def extract_text(body: bytes, filename: str, content_type: str) -> tuple[str, str | None]:
    suffix = PurePosixPath(filename).suffix.lower()
    if content_type == "application/pdf" or suffix == ".pdf":
        try:
            pdf = PdfReader(io.BytesIO(body))
            if pdf.is_encrypted and not pdf.decrypt(""):
                raise GradescopeError("This PDF is encrypted.")
            if len(pdf.pages) > 300:
                raise GradescopeError("PDF exceeds the 300-page extraction limit.")
            text = "\n\n".join(
                f"[Page {i + 1}]\n{page.extract_text() or ''}" for i, page in enumerate(pdf.pages)
            )
            return (
                text,
                "PDF text extraction does not read scanned images, diagrams, or handwriting.",
            )
        except GradescopeError:
            raise
        except Exception:
            raise GradescopeError("Could not extract PDF text.") from None
    if suffix in (".docx", ".pptx"):
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                entries = archive.infolist()
                if len(entries) > 5000 or sum(x.file_size for x in entries) > 50 * 1024 * 1024:
                    raise GradescopeError("Office document exceeds the extraction limit.")
                names = [
                    n
                    for n in archive.namelist()
                    if (
                        re.fullmatch(
                            r"word/(document|footnotes|endnotes|header\d+|footer\d+)\.xml", n
                        )
                        or re.fullmatch(r"ppt/(slides/slide|notesSlides/notesSlide)\d+\.xml", n)
                    )
                ]
                names.sort(key=lambda n: re.sub(r"\d+", lambda m: m[0].zfill(8), n))
                sections = []
                for name in names:
                    root = ElementTree.fromstring(archive.read(name))
                    sections.append(
                        "\n".join(
                            " ".join(t.text or "" for t in p.iter() if t.tag.endswith("}t"))
                            for p in root.iter()
                            if p.tag.endswith("}p")
                        )
                    )
                return "\n\n".join(
                    sections
                ), "Text only; embedded images and layout are not extracted."
        except GradescopeError:
            raise
        except (zipfile.BadZipFile, ElementTree.ParseError, KeyError):
            raise GradescopeError("Could not extract Office document text.") from None
    if content_type.startswith("text/") or suffix in {
        ".txt",
        ".md",
        ".csv",
        ".tsv",
        ".json",
        ".ipynb",
        ".py",
        ".r",
        ".tex",
        ".html",
        ".htm",
        ".xml",
        ".js",
        ".css",
        ".yaml",
        ".yml",
        ".sql",
    }:
        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise GradescopeError(
                "This text file is not UTF-8; automatic extraction is unavailable."
            ) from None
        if content_type == "text/html" or suffix in (".html", ".htm"):
            text = html_text(text)
        return text, None
    raise GradescopeError("Text extraction supports PDF, DOCX, PPTX, HTML, and UTF-8 text files.")
