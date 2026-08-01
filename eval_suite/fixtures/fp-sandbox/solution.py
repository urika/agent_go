def validate_email(email: str) -> bool:
    """Validate an email address according to specified rules.

    Rules:
    - Must contain exactly one '@'
    - At least one character before '@'
    - At least one character after '@'
    - Domain part (after '@') must contain at least one '.'
    - No spaces allowed
    - Empty string returns False
    """
    if not email:
        return False

    if ' ' in email:
        return False

    if email.count('@') != 1:
        return False

    local, domain = email.split('@')

    if not local or not domain:
        return False

    if '.' not in domain:
        return False

    return True


def batch_validate(emails: list[str]) -> list[bool]:
    """Validate a list of email addresses and return a list of booleans."""
    return [validate_email(e) for e in emails]
