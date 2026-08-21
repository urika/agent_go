from src.slugify import slugify


def test_basic_conversion():
    assert slugify('Hello World') == 'hello-world'


def test_special_characters():
    assert slugify('Hello! World?') == 'hello-world'


def test_edge_cases():
    assert slugify('  --Test--  ') == 'test'
