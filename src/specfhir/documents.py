"""Source-located text extraction. No generated answers or FHIR interpretation."""

import hashlib
import re
from html.parser import HTMLParser

from specfhir.files import checksum as file_checksum
from specfhir.files import download

MAX_CHARS = 4000
TEXT_FIELDS = ("short", "definition", "comment", "requirements")


class Narrative(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"p", "div", "br", "tr", "li", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag in {"p", "div", "tr", "li"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def chunks(text: str):
    """Paragraph/word boundaries, with a hard bound for unbroken source strings."""
    text = re.sub(r"[^\S\n]+", " ", text).strip()
    while text:
        end = len(text)
        if end > MAX_CHARS:
            end = text.rfind("\n", 0, MAX_CHARS + 1)
            if end < MAX_CHARS // 2:
                end = text.rfind(" ", 0, MAX_CHARS + 1)
            if end < MAX_CHARS // 2:
                end = MAX_CHARS
        yield text[:end].strip()
        text = text[end:].strip()


def extract(resource: dict, issues: dict):
    heading = " ".join(str(resource.get(k, "")) for k in ("name", "title", "id"))
    sections = []
    if resource.get("resourceType") == "Documentation" and "sections" in resource:
        for i, section in enumerate(resource["sections"]):
            sections.append(
                (
                    "section",
                    f"/sections/{i}/text",
                    None,
                    None,
                    heading + " " + section["heading"],
                    section["text"],
                )
            )
    for key in ("description", "purpose", "copyright"):
        if (
            key == "description"
            and resource.get("resourceType") == "Documentation"
            and "sections" in resource
        ):
            continue
        if isinstance(value := resource.get(key), str) and value.strip():
            sections.append((key, f"/{key}", None, None, heading, value))
    narrative = resource.get("text", {}).get("div")
    if isinstance(narrative, str):
        parser = Narrative()
        parser.feed(narrative)
        sections.append(("narrative", "/text/div", None, None, heading, "".join(parser.parts)))
    if resource.get("resourceType") == "StructureDefinition":
        view = (
            "snapshot" if resource.get("snapshot") and "snapshot" not in issues else "differential"
        )
        if view not in issues:
            for ordinal, element in enumerate(resource.get(view, {}).get("element", [])):
                parts = [f"{key}: {element[key]}" for key in TEXT_FIELDS if element.get(key)]
                binding = element.get("binding", {})
                if binding:
                    parts.append(
                        "binding: "
                        + " ".join(
                            str(binding.get(k, "")) for k in ("strength", "description", "valueSet")
                        )
                    )
                if element.get("slicing"):
                    parts.append("slicing: " + str(element["slicing"].get("rules", "")))
                if parts:
                    sections.append(
                        (
                            "element",
                            f"/{view}/element/{ordinal}",
                            element["id"],
                            view,
                            heading + " " + element["id"],
                            "\n".join(parts),
                        )
                    )
    seen = set()
    for kind, pointer, element_id, view, title, text in sections:
        for ordinal, passage in enumerate(chunks(text)):
            checksum = hashlib.sha256(passage.encode()).hexdigest()
            # Preserve distinct element provenance, remove repeated chunks at the same locator.
            if (pointer, checksum) in seen:
                continue
            seen.add((pointer, checksum))
            yield [kind, pointer, element_id, view, ordinal, title, passage, checksum]


def page_sections(html: str, anchors=()) -> list[dict]:
    """Preserve authored heading boundaries, anchors and links inside the content region."""

    class Page(Narrative):
        def __init__(self):
            super().__init__()
            self.depth = 0
            self.found = False
            self.sections = []
            self.heading = ""
            self.anchor = None
            self.pending_anchor = None
            self.in_heading = False
            self.links = []

        def flush(self):
            text = "".join(self.parts).strip()
            if text:
                self.sections.append(
                    dict(
                        heading=self.heading,
                        anchor=self.anchor,
                        text=text,
                        links=list(dict.fromkeys(self.links)),
                    )
                )
            self.parts = []
            self.links = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "div" and attrs.get("id") == "segment-content":
                self.depth = 1
                self.found = True
                return
            if not self.depth:
                return
            if tag == "div":
                self.depth += 1
            if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                self.flush()
                self.heading = ""
                self.anchor = attrs.get("id") or self.pending_anchor
                self.pending_anchor = None
                self.in_heading = True
            elif attrs.get("id") or (tag == "a" and attrs.get("name")):
                anchor = attrs.get("id") or attrs.get("name")
                if self.in_heading:
                    self.anchor = self.anchor or anchor
                elif tag == "a":
                    self.pending_anchor = anchor
            if tag == "a" and attrs.get("href"):
                self.links.append(attrs["href"])
            super().handle_starttag(tag, list(attrs.items()))

        def handle_endtag(self, tag):
            if not self.depth:
                return
            if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                self.heading = " ".join("".join(self.parts).split())
                self.parts.append("\n")
                self.in_heading = False
            super().handle_endtag(tag)
            if tag == "div":
                self.depth -= 1

        def handle_data(self, data):
            if self.depth:
                super().handle_data(data)
                if data.strip() and not self.in_heading:
                    self.pending_anchor = None

    parser = Page()
    parser.feed(html)
    parser.flush()
    if not parser.found or not parser.sections:
        raise ValueError("Published page has no IG segment-content region")
    if anchors:
        selected = [s for s in parser.sections if s["anchor"] in anchors]
        if sorted(s["anchor"] for s in selected) != sorted(anchors) or len(set(anchors)) != len(
            anchors
        ):
            raise ValueError("Selected publication anchors are missing, ambiguous, or duplicated")
        return selected
    return parser.sections


def page_text(html: str) -> str:
    return "\n\n".join(section["text"] for section in page_sections(html))


def pin_pages(sources, previous, cache):
    """Pin explicitly configured publication pages; never crawl or select latest."""
    import httpx

    from specfhir.models import DocumentPin, Error

    old = {p.url: p for p in previous} if previous is not None else None
    if old is not None and set(old) != {s.url for s in sources}:
        raise Error("Documentation config/lock mismatch; run sync --update-lock")
    cache.mkdir(parents=True, exist_ok=True)
    pins = []
    for source in sources:
        pin = old[source.url] if old is not None else None
        path = cache / f"{pin.sha256}.html" if pin else None
        if pin and any(getattr(pin, k) != v for k, v in source.model_dump().items()):
            raise Error("Documentation config/lock mismatch; run sync --update-lock")
        if path is None or not path.exists():
            content = bytearray()
            with httpx.stream("GET", source.url, follow_redirects=True, timeout=60) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 2 * 1024 * 1024:
                        raise Error("Documentation page exceeds 2 MiB")
            sha = hashlib.sha256(content).hexdigest()
            if pin and pin.sha256 != sha:
                raise Error(f"Published documentation checksum changed: {source.url}")
            page_sections(content.decode("utf-8"), source.anchors)
            path = cache / f"{sha}.html"
            temporary = path.with_suffix(".tmp")
            temporary.write_bytes(content)
            temporary.replace(path)
        sha = file_checksum(path)
        if pin and pin.sha256 != sha:
            raise Error(f"Cached documentation checksum changed: {source.url}")
        pins.append(DocumentPin(**source.model_dump(), sha256=sha))
    return pins


def page_candidates(archive, key):
    """Discover narrative pages from the exact IG resource, never from crawled links."""
    import json
    from pathlib import PurePosixPath
    from urllib.parse import urlsplit

    from specfhir.models import Error
    from specfhir.packages import archive_files

    guide = None
    rendered_resources = set()
    for name, raw in archive_files(archive):
        if not name.startswith("package/") or name.count("/") != 1 or not name.endswith(".json"):
            continue
        rendered_resources.add(PurePosixPath(name).stem + ".html")
        if name.startswith("package/ImplementationGuide-"):
            resource = json.loads(raw)
            if (resource.get("packageId"), resource.get("version")) == tuple(key.split("#")):
                if guide is not None:
                    raise Error(f"Multiple ImplementationGuide resources for {key}")
                guide = resource
    if guide is None or not guide.get("definition", {}).get("page"):
        raise Error(f"No ImplementationGuide page hierarchy for {key}")
    candidates = []
    seen = set()
    administrative = {
        "toc.html",
        "downloads.html",
        "credits.html",
        "changes.html",
        "changelog.html",
        "artifacts.html",
    }

    def visit(page, depth=0):
        if depth > 64 or len(candidates) >= 1024:
            raise Error("IG page hierarchy exceeds limits")
        if not isinstance(page, dict):
            raise Error("IG page entry must be an object")
        name = page.get("nameUrl")
        title = page.get("title")
        if not isinstance(name, str) or not isinstance(title, str) or not title.strip():
            raise Error("IG page entry requires nameUrl and title")
        url = urlsplit(name)
        path = PurePosixPath(url.path)
        reason = None
        if url.scheme or url.netloc:
            reason = "External link"
        elif (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in name
            or "%" in name
            or url.query
            or url.fragment
            or str(path) != name
        ):
            raise Error(f"Unsafe or unsupported IG page path: {name}")
        elif path.suffix != ".html":
            reason = "Not an HTML publication page"
        elif name in rendered_resources:
            reason = "Resource already available as JSON"
        elif path.name.lower() in administrative:
            reason = "Navigation, downloads, credits, or release administration"
        if name not in seen:
            seen.add(name)
            candidates.append(
                {
                    "path": name,
                    "title": title,
                    "selected": reason is None,
                    "excluded_reason": reason,
                }
            )
        for child in page.get("page", []):
            visit(child, depth + 1)

    visit(guide["definition"]["page"])
    return candidates


def pin_publications(sources, previous, packages, work):
    """Pin bounded publication ZIPs and selected pages, preserving their release provenance."""
    import stat
    import zipfile
    from pathlib import PurePosixPath
    from urllib.parse import urljoin

    from specfhir.models import DocumentPin, Error, PublicationPin

    old = {p.package: p for p in previous.publications} if previous is not None else None
    if previous is not None and [
        p.model_dump(exclude={"sha256"}) for p in previous.publications
    ] != [s.model_dump() for s in sources]:
        raise Error("Publication config/lock mismatch; run sync --update-lock")
    cache = work / "publications"
    cache.mkdir(parents=True, exist_ok=True)
    page_cache = work / "documents"
    page_cache.mkdir(exist_ok=True)
    pins, pages = [], []
    for source in sources:
        pin = old[source.package] if old is not None else None
        path = cache / (hashlib.sha256(source.url.encode()).hexdigest() + ".zip")
        sha = download(path, source.url, pin.sha256 if pin else None, 256 * 1024 * 1024)
        package = next(p for p in packages if p.key == source.package)
        package_path = work / "packages" / f"{package.key}.tgz"
        if file_checksum(package_path) != package.sha256:
            raise Error(f"Package checksum changed: {package.key}")
        candidates = page_candidates(package_path, package.key)
        selected = []
        try:
            with zipfile.ZipFile(path) as archive:
                names = set()
                expanded = 0
                for entry in archive.infolist():
                    name = PurePosixPath(entry.filename)
                    mode = entry.external_attr >> 16
                    if (
                        name.is_absolute()
                        or ".." in name.parts
                        or "\\" in entry.filename
                        or entry.filename.rstrip("/") != str(name)
                        or str(name) in names
                        or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)))
                        or entry.flag_bits & 1
                    ):
                        raise Error(f"Unsafe publication archive member: {entry.filename}")
                    names.add(str(name))
                    expanded += entry.file_size
                    if (
                        entry.file_size > 256 * 1024 * 1024
                        or expanded > 2 * 1024**3
                        or len(names) > 100_000
                    ):
                        raise Error("Publication archive limits exceeded")
                roots = [prefix for prefix in ("", "site/") if prefix + "package.tgz" in names]
                if len(roots) != 1:
                    raise Error("Publication must contain one root or site/package.tgz")
                root = roots[0]
                if hashlib.sha256(archive.read(root + "package.tgz")).hexdigest() != package.sha256:
                    raise Error(f"Publication embedded package differs from locked {package.key}")
                for candidate in candidates:
                    if not candidate["selected"]:
                        continue
                    relative = source.page_prefix + candidate["path"]
                    member = root + relative
                    if member not in names:
                        raise Error(f"Selected IG page missing from publication: {member}")
                    if archive.getinfo(member).file_size > 2 * 1024 * 1024:
                        raise Error(f"Publication page exceeds 2 MiB: {member}")
                    raw = archive.read(member)
                    page_text(raw.decode("utf-8"))
                    page_sha = hashlib.sha256(raw).hexdigest()
                    page = DocumentPin(
                        package=source.package,
                        url=urljoin(source.url, relative),
                        title=candidate["title"],
                        sha256=page_sha,
                        publication=source.url,
                        member=member,
                    )
                    target = page_cache / f"{page_sha}.html"
                    if target.exists() and file_checksum(target) != page_sha:
                        raise Error(f"Cached documentation checksum changed: {page.url}")
                    if not target.exists():
                        temporary = target.with_suffix(".tmp")
                        temporary.write_bytes(raw)
                        temporary.replace(target)
                    selected.append(page)
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
            raise Error(f"Invalid publication archive: {source.url}: {exc}") from exc
        if previous is not None and selected != [
            p for p in previous.documents if p.publication == source.url
        ]:
            raise Error("Discovered publication pages differ from lock; run sync --update-lock")
        if not selected:
            raise Error(f"No narrative pages selected for {source.package}")
        pins.append(PublicationPin(**source.model_dump(), sha256=sha))
        pages.extend(selected)
    return pins, pages
