"""DocBase retrieval use cases, imported lazily to keep doc2md independent."""


def build_index(*args, **kwargs):
    from docqa.docbase.pipeline import build_index as implementation

    return implementation(*args, **kwargs)


def query(*args, **kwargs):
    from docqa.docbase.retrieval import query as implementation

    return implementation(*args, **kwargs)


def eval_retrieval(*args, **kwargs):
    from docqa.docbase.evaluation import eval_retrieval as implementation

    return implementation(*args, **kwargs)
