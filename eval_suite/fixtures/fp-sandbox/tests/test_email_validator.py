from email_validator import validate_email


def test_valid_email():
    assert validate_email('user@example.com') is True


def test_invalid_email_no_at():
    assert validate_email('userexample.com') is False


def test_invalid_email_no_dot_in_domain():
    assert validate_email('user@domain') is False
