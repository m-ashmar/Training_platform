"""The POST a browser would send for an admin change form, read from its rendered page.

A console test that edits one field has to send every other field the page holds as it
was (the user form alone has dozens, and every inline its management form), or Django
reads the missing ones as cleared. This reads them from the HTML the GET returned.
"""
from html.parser import HTMLParser


class _Form(HTMLParser):
    def __init__(self):
        super().__init__()
        self.data, self._select, self._textarea, self._text = {}, None, None, []
        self._multiple, self._first = False, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        name = a.get("name")
        if tag == "input" and name:
            kind = a.get("type", "text")
            if kind in ("submit", "button", "image", "file"):
                return
            if kind in ("checkbox", "radio"):
                if "checked" in a:
                    self.data.setdefault(name, []).append(a.get("value", "on"))
                return
            self.data.setdefault(name, []).append(a.get("value", ""))
        elif tag == "select" and name:
            self._select, self._multiple, self._first = name, "multiple" in a, None
            self.data.setdefault(name, [])
        elif tag == "option" and self._select:
            if self._first is None:
                self._first = a.get("value", "")
            if "selected" in a:
                self.data[self._select].append(a.get("value", ""))
        elif tag == "textarea" and name:
            self._textarea, self._text = name, []

    def handle_endtag(self, tag):
        if tag == "select" and self._select:
            # A browser sends the first option of a single select with none selected,
            # and nothing at all for an empty multiple one.
            if not self.data[self._select]:
                if self._multiple:
                    del self.data[self._select]
                elif self._first is not None:
                    self.data[self._select].append(self._first)
            self._select = None
        elif tag == "textarea" and self._textarea:
            self.data.setdefault(self._textarea, []).append("".join(self._text))
            self._textarea = None

    def handle_data(self, data):
        if self._textarea:
            self._text.append(data)


def change_form_data(html: str) -> dict:
    """Every field of the page's form as the browser would POST it (lists for repeats)."""
    parser = _Form()
    parser.feed(html)
    return {name: (values if len(values) > 1 else (values[0] if values else ""))
            for name, values in parser.data.items()}
