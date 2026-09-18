import base64
from html.parser import HTMLParser


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        value = data.strip()
        if value:
            self.parts.append(value)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.parts.append(href)

    def text(self):
        return "\n".join(self.parts)


def decoded_message_text(payload):
    candidates = []

    def visit(part):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime in ("text/plain", "text/html"):
            raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
            if mime == "text/html":
                parser = HTMLText()
                parser.feed(raw)
                raw = parser.text()
            score = sum(marker in raw for marker in ("Confirmation code", "Total (USD)", "airbnb.com/rooms/", "Check-in", "Checkout"))
            candidates.append((score, len(raw), raw))
        for child in part.get("parts", []):
            visit(child)

    visit(payload)
    return max(candidates, default=(0, 0, ""))[2]
