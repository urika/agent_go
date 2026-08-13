import re


def validate_email(email):
    if email is None or not isinstance(email, str):
        return False
    email = email.strip()
    return bool(re.match(r'^[^@]+@[^@]+\.[^@]+$', email))
