"""Multiformat document conversion; public functions are loaded lazily."""


def parse_document(*args, **kwargs):
    from docqa.doc2md.pipeline import parse_document as implementation

    return implementation(*args, **kwargs)


def batch_parse(*args, **kwargs):
    from docqa.doc2md.pipeline import batch_parse as implementation

    return implementation(*args, **kwargs)
