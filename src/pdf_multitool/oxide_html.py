"""Render untrusted native HTML as a restricted, app-owned content fragment."""

import html
from html.parser import HTMLParser

from .core import JobError


class _Fragment(HTMLParser):
    allowed = frozenset(
        {
            "p",
            "div",
            "span",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "table",
            "thead",
            "tbody",
            "tfoot",
            "tr",
            "td",
            "th",
            "caption",
            "ul",
            "ol",
            "li",
            "strong",
            "em",
            "b",
            "i",
            "u",
            "s",
            "sub",
            "sup",
            "pre",
            "code",
            "blockquote",
            "br",
            "hr",
        }
    )
    suppressed = frozenset(
        {"head", "script", "style", "iframe", "object", "svg", "math", "template"}
    )

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.stack = []
        self.hidden = []
        self.removed_tags = self.removed_attributes = 0

    def handle_starttag(self, tag, attrs):
        self.removed_attributes += len(attrs)
        if self.hidden or tag in self.suppressed:
            if len(self.hidden) >= 64:
                raise JobError("PDF Oxide HTML nesting exceeds its limit")
            if tag not in {"img", "meta", "link", "br", "hr", "input", "source", "wbr"}:
                self.hidden.append(tag)
            self.removed_tags += 1
        elif tag in self.allowed:
            if len(self.stack) >= 64:
                raise JobError("PDF Oxide HTML nesting exceeds its limit")
            self.parts.append(f"<{tag}>")
            if tag not in {"br", "hr"}:
                self.stack.append(tag)
        else:
            self.removed_tags += 1

    def handle_endtag(self, tag):
        if self.hidden:
            if tag in self.hidden:
                del self.hidden[self.hidden.index(tag) :]
        elif tag in self.stack:
            while self.stack:
                current = self.stack.pop()
                self.parts.append(f"</{current}>")
                if current == tag:
                    break

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(html.escape(data))


def sanitize(value):
    parser = _Fragment()
    parser.feed(value)
    parser.close()
    parser.parts.extend(f"</{tag}>" for tag in reversed(parser.stack))
    return {
        "fragment": "".join(parser.parts),
        "removed_tags": parser.removed_tags,
        "removed_attributes": parser.removed_attributes,
        "policy": (
            "Allowlisted structural tags; all attributes, links, images, active content "
            "and native CSS removed"
        ),
    }
