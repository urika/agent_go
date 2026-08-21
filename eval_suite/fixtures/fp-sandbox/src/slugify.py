import re


def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r'[^a-z0-9-]+', '-', text)
    text = re.sub(r'-+', '-', text)
    return text.strip('-')
