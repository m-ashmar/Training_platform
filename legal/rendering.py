"""An operator's plain text, as safe HTML.

The body of a legal document is typed in the console, and the public page must never
run what someone typed there, so nothing is passed through as markup: every character
is escaped first, and only these shapes become HTML —

* a blank line ends a paragraph, and a single line break inside one is kept;
* a paragraph whose first line starts with `## ` (or `# `) is a heading;
* a paragraph whose lines all start with `- ` is a bulleted list;
* a web or email address becomes a link (`django.utils.html.urlize`, which escapes).
"""
from django.utils.html import escape, urlize
from django.utils.safestring import mark_safe


def _inline(text: str) -> str:
    # `urlize` escapes the text around the links it makes, and `nofollow` keeps an
    # operator's link from lending the site's standing to wherever it points.
    return urlize(text, nofollow=True, autoescape=True)


def render_body(text: str) -> str:
    blocks = []
    for raw in (text or "").replace("\r\n", "\n").split("\n\n"):
        lines = [line.rstrip() for line in raw.strip("\n").split("\n") if line.strip()]
        if not lines:
            continue
        first = lines[0].lstrip()
        if first.startswith("#") and first.lstrip("#").startswith(" "):
            title = " ".join(line.strip() for line in lines).lstrip("#").strip()
            blocks.append(f"<h2>{escape(title)}</h2>")
        elif all(line.lstrip().startswith("- ") for line in lines):
            items = "".join(f"<li>{_inline(line.lstrip()[2:].strip())}</li>"
                            for line in lines)
            blocks.append(f"<ul>{items}</ul>")
        else:
            blocks.append("<p>" + "<br>".join(_inline(line.strip()) for line in lines)
                          + "</p>")
    return mark_safe("\n".join(blocks))
