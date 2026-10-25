"""Deterministic Chinese/identifier analysis for the BM25 route only."""

from __future__ import annotations

import importlib.metadata
import re
import threading
import unicodedata
from collections import OrderedDict
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType

import jieba

from docqa.domain.errors import DocQAError

# Full ASCII technical terms are protected before Chinese segmentation. Keeping
# the entire identifier prevents FE_HEART_Start / --flag from losing specificity.
_IDENTIFIER = re.compile(
    r"(?:https?://[^\s<>\"']+|[A-Za-z]:[\\/][^\s<>\"']+|"
    r"--?[A-Za-z][A-Za-z0-9_-]*(?:=[A-Za-z0-9_.:/\\-]+)?|"
    r"[A-Za-z0-9_]+(?:[.:/\\@-][A-Za-z0-9_]+)*)"
)

# Only frozen dictionary views are shared. Each Analyzer keeps its own immutable
# terminology/pattern and budget; callers never receive the mutable Tokenizer.
_TOKENIZER_LOCK = threading.Lock()
_TOKENIZER_CACHE: OrderedDict[tuple[str, str, tuple[str, ...]], Callable] = OrderedDict()
_TOKENIZER_CACHE_LIMIT = 32


def _frozen_cut(dictionary: Path, dictionary_digest: str, version: str, terms: tuple[str, ...]):
    key = (dictionary_digest, version, terms)
    with _TOKENIZER_LOCK:
        cached = _TOKENIZER_CACHE.get(key)
        if cached is not None:
            _TOKENIZER_CACHE.move_to_end(key)
            return cached
        tokenizer = jieba.Tokenizer(str(dictionary))
        tokenizer.initialize()
        for term in terms:
            tokenizer.add_word(term, freq=10**9)
        if sha256(dictionary.read_bytes()).hexdigest() != dictionary_digest:
            raise DocQAError(
                "ANALYZER_DICTIONARY_CHANGED", "Analyzer dictionary changed during initialization"
            )
        # HMM=False cut only reads FREQ/total. Freezing these mappings prevents
        # accidental add_word/del_word mutations from crossing Analyzer users.
        tokenizer.FREQ = MappingProxyType(tokenizer.FREQ)
        tokenizer.user_word_tag_tab = MappingProxyType(tokenizer.user_word_tag_tab)

        def cut(text: str) -> list[str]:
            return list(tokenizer.cut(text, HMM=False))

        _TOKENIZER_CACHE[key] = cut
        while len(_TOKENIZER_CACHE) > _TOKENIZER_CACHE_LIMIT:
            _TOKENIZER_CACHE.popitem(last=False)
        return cut


class Analyzer:
    """Own a frozen jieba tokenizer; never mutate jieba's process-global state.

    A generation binds the dictionary bytes, exact package version, terminology,
    normalization and HMM policy. Query code compares this profile to the index.
    No default stopwords are removed, in particular Chinese negation is retained.
    """

    def __init__(
        self,
        terms: tuple[str, ...] | list[str] = (),
        *,
        max_token_utf8_bytes: int = 4096,
    ):
        if not 16 <= max_token_utf8_bytes <= 1024 * 1024:
            raise DocQAError("INVALID_CONFIG", "max_token_utf8_bytes must be 16..1048576")
        self.max_token_utf8_bytes = max_token_utf8_bytes
        normalized = tuple(sorted({unicodedata.normalize("NFKC", t).casefold() for t in terms}))
        if any(not t.strip() or len(t.encode("utf-8")) > max_token_utf8_bytes for t in normalized):
            raise DocQAError("INVALID_CONFIG", "analyzer terminology is empty or over budget")
        self._terms = normalized
        dictionary = Path(jieba.__file__).with_name("dict.txt")
        self.dictionary_sha256 = sha256(dictionary.read_bytes()).hexdigest()
        self._jieba_version = importlib.metadata.version("jieba")
        self._cut = _frozen_cut(dictionary, self.dictionary_sha256, self._jieba_version, normalized)
        # Longest-first explicit protection also handles terms containing spaces.
        escaped = [re.escape(term) for term in sorted(normalized, key=lambda t: (-len(t), t))]
        self._term_pattern = re.compile("|".join(escaped)) if escaped else None

    @property
    def terms(self) -> tuple[str, ...]:
        return self._terms

    def profile(self) -> dict:
        return {
            "schema_version": "docqa.analyzer.v1",
            "jieba_version": self._jieba_version,
            "dictionary_sha256": self.dictionary_sha256,
            "code_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
            "normalization": "NFKC+casefold",
            "identifier_rule": "ascii-technical-v1",
            "terms": list(self.terms),
            "hmm": False,
            "stopwords": [],
            "max_token_utf8_bytes": self.max_token_utf8_bytes,
        }

    def _cut_chinese(self, text: str) -> list[str]:
        output: list[str] = []
        position = 0
        if self._term_pattern:
            for match in self._term_pattern.finditer(text):
                output.extend(self._cut(text[position : match.start()]))
                output.append(match.group())
                position = match.end()
        output.extend(self._cut(text[position:]))
        return output

    def _segment(self, text: str) -> list[str]:
        output: list[str] = []
        position = 0
        for match in _IDENTIFIER.finditer(text):
            output.extend(self._cut_chinese(text[position : match.start()]))
            output.append(match.group())
            position = match.end()
        output.extend(self._cut_chinese(text[position:]))
        return output

    def analyze(self, text: str) -> list[str]:
        if not isinstance(text, str):
            raise DocQAError("INVALID_INPUT", "analyzer input must be a string")
        view = unicodedata.normalize("NFKC", text).casefold()
        output = self._segment(view)
        tokens = [token for token in output if any(char.isalnum() for char in token)]
        if any(len(token.encode("utf-8")) > self.max_token_utf8_bytes for token in tokens):
            raise DocQAError("TOKEN_TOO_LONG", "an identifier exceeds the analyzer byte budget")
        return tokens

    __call__ = analyze
