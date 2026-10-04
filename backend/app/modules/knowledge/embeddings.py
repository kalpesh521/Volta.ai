"""
Embedding port.

The default embedder is a local feature hash. It needs no API key, is stable
across processes, and is what tests and offline installs use. When
`AI_EMBEDDING_PROVIDER` and a key are set, documents are embedded with that
provider instead. Retrieval only compares vectors from the same model name.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Protocol

from app.ai.config import AISettings

logger = logging.getLogger("volta.knowledge.embeddings")

_TOKEN = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    model_name: str
    dimensions: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class HashingEmbedder:
    """Signed feature hashing (unigrams + bigrams), L2-normalised."""

    def __init__(self, dimensions: int = 256, model_name: str = "hashing-256") -> None:
        self.dimensions = dimensions
        self.model_name = model_name

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        tokens = _TOKEN.findall(text.lower())
        grams = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]
        for gram in grams:
            digest = hashlib.sha256(gram.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "little") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[bucket] += sign
        return _normalise(vec)


def build_embedder(settings: AISettings) -> Embedder:
    provider = settings.AI_EMBEDDING_PROVIDER.strip().lower()
    model = settings.AI_EMBEDDING_MODEL.strip() or "hashing-256"
    dimensions = settings.AI_EMBEDDING_DIMENSIONS
    if not provider:
        return HashingEmbedder(dimensions, model if model.startswith("hashing") else f"hashing-{dimensions}")
    remote = _remote_embedder(provider, model, dimensions, settings)
    if remote is None:
        logger.warning("embedding provider %s is not usable; using the local hashing embedder", provider)
        return HashingEmbedder(dimensions, f"hashing-{dimensions}")
    return remote


def _remote_embedder(provider: str, model: str, dimensions: int, settings: AISettings) -> Embedder | None:
    if provider in {"openai", "groq"}:
        key = settings.OPENAI_API_KEY if provider == "openai" else settings.GROQ_API_KEY
        if key is None or not key.get_secret_value().strip():
            return None
        try:
            from langchain_openai import OpenAIEmbeddings
        except ImportError:
            logger.warning("langchain-openai is not installed")
            return None
        kwargs: dict = {"model": model, "api_key": key.get_secret_value()}
        if provider == "groq":
            return None
        if dimensions:
            kwargs["dimensions"] = dimensions
        client = OpenAIEmbeddings(**kwargs)
        return _LangChainEmbedder(client, model_name=f"openai:{model}", dimensions=dimensions)
    if provider == "google_genai":
        key = settings.GOOGLE_API_KEY
        if key is None or not key.get_secret_value().strip():
            return None
        try:
            from langchain_google_genai import GoogleGenerativeAIEmbeddings
        except ImportError:
            logger.warning("langchain-google-genai embeddings are not installed")
            return None
        client = GoogleGenerativeAIEmbeddings(model=model, google_api_key=key.get_secret_value())
        return _LangChainEmbedder(client, model_name=f"google:{model}", dimensions=dimensions)
    return None


class _LangChainEmbedder:
    def __init__(self, client: object, *, model_name: str, dimensions: int) -> None:
        self._client = client
        self.model_name = model_name
        self.dimensions = dimensions

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = await self._client.aembed_documents(texts)  # type: ignore[attr-defined]
        return [_fit(list(map(float, vector)), self.dimensions) for vector in vectors]

    async def embed_query(self, text: str) -> list[float]:
        vector = await self._client.aembed_query(text)  # type: ignore[attr-defined]
        return _fit(list(map(float, vector)), self.dimensions)


def _fit(vector: list[float], dimensions: int) -> list[float]:
    if len(vector) == dimensions:
        return _normalise(vector)
    if len(vector) > dimensions:
        return _normalise(vector[:dimensions])
    return _normalise(vector + [0.0] * (dimensions - len(vector)))


def _normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]
