"""Source-located text extraction. No generated answers or FHIR interpretation."""

import hashlib
import re
from html.parser import HTMLParser

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
    for key in ("description", "purpose", "copyright"):
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


def page_text(html: str) -> str:
    """Extract the published IG content region, excluding shared navigation/footer."""

    class Page(Narrative):
        depth = 0
        found = False

        def handle_starttag(self, tag, attrs):
            if tag == "div" and dict(attrs).get("id") == "segment-content":
                self.depth = 1
                self.found = True
                return
            if self.depth:
                if tag == "div":
                    self.depth += 1
                super().handle_starttag(tag, attrs)

        def handle_endtag(self, tag):
            if self.depth:
                super().handle_endtag(tag)
                if tag == "div":
                    self.depth -= 1

        def handle_data(self, data):
            if self.depth:
                super().handle_data(data)

    parser = Page()
    parser.feed(html)
    text = "".join(parser.parts).strip()
    if not parser.found or not text:
        raise ValueError("Published page has no IG segment-content region")
    return text


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
            page_text(content.decode("utf-8"))
            path = cache / f"{sha}.html"
            temporary = path.with_suffix(".tmp")
            temporary.write_bytes(content)
            temporary.replace(path)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if pin and pin.sha256 != sha:
            raise Error(f"Cached documentation checksum changed: {source.url}")
        pins.append(DocumentPin(**source.model_dump(), sha256=sha))
    return pins
