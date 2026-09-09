"""Pinned FastEmbed inference. Only sync may download model files."""

import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from functools import lru_cache
from importlib.metadata import version
from itertools import batched
from pathlib import Path

import numpy as np
from huggingface_hub import snapshot_download
from tokenizers import Tokenizer

from specfhir.config import digest
from specfhir.files import checksum
from specfhir.models import Error

MODEL = "BAAI/bge-small-en-v1.5"
REPO = "Qdrant/bge-small-en-v1.5-onnx-Q"
REVISION = "52398278842ec682c6f32300af41344b1c0b0bb2"
FILES = (
    "config.json",
    "model_optimized.onnx",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.txt",
)
DIMENSIONS = 384
# Bump for spool changes; also bump db.SCHEMA_VERSION if published content changes.
PREPARATION_VERSION = 1


def cache_key(pin: dict) -> str:
    return "embedding-" + digest({"pin": pin, "format": PREPARATION_VERSION})


def prepare_cached(work: Path, source: Path, pin: dict):
    """Reuse verified embedded passages within an exact package-preparation entry."""
    target = source.parent / cache_key(pin)
    try:
        metadata = json.loads((target / "metadata.json").read_text())
        if checksum(target / "artifacts.documents") == metadata["sha256"]:
            counts = metadata["counts"]
            return (
                target / "artifacts.documents",
                {**counts, "embedding_cache_hits": counts["embeddings"]},
                True,
            )
    except (OSError, ValueError, KeyError, TypeError):
        pass
    with tempfile.TemporaryDirectory(dir=source.parent) as temporary:
        staging = Path(temporary) / "prepared"
        staging.mkdir()
        spool = staging / "artifacts.jsonl"
        shutil.copyfile(source, spool)
        shutil.copyfile(source.with_suffix(".documents"), spool.with_suffix(".documents"))
        counts = prepare(work, spool, pin)
        spool.unlink()
        (staging / "metadata.json").write_text(
            json.dumps({"counts": counts, "sha256": checksum(spool.with_suffix(".documents"))})
        )
        if target.exists():
            shutil.rmtree(target)
        staging.replace(target)
    return target / "artifacts.documents", counts, False


def pin_model(work: Path, settings, previous: dict | None, update: bool) -> dict:
    identity = {
        "model": settings.model,
        "repo": REPO,
        "revision": settings.revision,
        "dimensions": DIMENSIONS,
        "max_tokens": settings.max_tokens,
        "fastembed": version("fastembed"),
        "onnxruntime": version("onnxruntime"),
        "tokenizers": version("tokenizers"),
        "format": 1,
        "semantic_exclusions": ["copyright", "generated_narrative"],
    }
    if previous and not update and any(previous.get(k) != v for k, v in identity.items()):
        raise Error("Embedding settings/runtime differ from lock; run sync --update-lock")
    directory = work / "models" / settings.revision
    if not all((directory / name).is_file() for name in FILES):
        snapshot_download(
            REPO, revision=settings.revision, allow_patterns=list(FILES), local_dir=directory
        )
    files = {name: checksum(directory / name) for name in FILES}
    if previous and not update and previous.get("files") != files:
        raise Error("Model checksum mismatch; previous index is unchanged")
    return {**identity, "files": files}


@lru_cache(maxsize=1)
def load_model(work: str, pin_json: str):
    from fastembed import TextEmbedding

    pin = json.loads(pin_json)
    if pin.get("model") != MODEL or pin.get("dimensions") != DIMENSIONS:
        raise Error("Unsupported indexed embedding model")
    for library in ("fastembed", "onnxruntime", "tokenizers"):
        if pin[library] != version(library):
            raise Error("Embedding runtime changed; sync --update-lock before semantic search")
    directory = Path(work) / "models" / pin["revision"]
    for name in FILES:
        if not (directory / name).is_file() or checksum(directory / name) != pin["files"][name]:
            raise Error("Pinned model files unavailable or changed; run specfhir sync")
    try:
        model = TextEmbedding(
            MODEL,
            specific_model_path=str(directory),
            local_files_only=True,
            threads=8,
            providers=["CPUExecutionProvider"],
        )
    except Exception as exc:
        raise Error(f"Unable to load pinned embedding model: {exc}") from exc
    tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return model, tokenizer


def bounded_passages(text, tokenizer, maximum):
    """Split original text on tokenizer offsets, retaining every source character."""
    offsets = tokenizer.encode(text, add_special_tokens=False).offsets
    start = cursor = 0
    while cursor < len(offsets):
        stop = min(cursor + maximum - 2, len(offsets))
        end = offsets[stop - 1][1]
        # WordPiece fragments can tokenize differently at a new chunk boundary.
        while len(tokenizer.encode(text[start:end]).ids) > maximum:
            stop -= 1
            if stop <= cursor:
                raise Error("Cannot split passage within token budget")
            end = offsets[stop - 1][1]
        passage = text[start:end]
        if passage.strip():
            yield passage
        start, cursor = end, stop


def vector(value):
    result = np.asarray(value, dtype=np.float32)
    if (
        result.shape != (DIMENSIONS,)
        or not np.isfinite(result).all()
        or np.linalg.norm(result) == 0
    ):
        raise Error("Invalid embedding output")
    return result.tolist()


def prepare(work: Path, spool: Path, pin: dict):
    model, tokenizer = load_model(str(work), json.dumps(pin, sort_keys=True))
    destination = spool.with_suffix(".embedded")
    generated = set()
    with spool.open() as stream:
        for line in stream:
            aid, _, _, resource, _ = json.loads(line)
            if resource.get("text", {}).get("status") == "generated":
                generated.add(aid)

    def inputs():
        with spool.with_suffix(".documents").open() as stream:
            for line in stream:
                row = json.loads(line)
                if row[1] == "copyright" or (row[1] == "narrative" and row[0] in generated):
                    yield row, None
                    continue
                heading = tokenizer.decode(
                    tokenizer.encode(row[6], add_special_tokens=False).ids[:48]
                )
                for part, passage in enumerate(
                    bounded_passages(row[7], tokenizer, pin["max_tokens"] - 56)
                ):
                    text = heading + "\n" + passage
                    if len(tokenizer.encode(text).ids) > pin["max_tokens"]:
                        raise Error("Passage exceeds model token budget")
                    yield (
                        row[:5]
                        + [
                            row[5] * 10000 + part,
                            row[6],
                            passage,
                            hashlib.sha256(passage.encode()).hexdigest(),
                        ],
                        text,
                    )

    identity = hashlib.sha256(json.dumps(pin, sort_keys=True).encode()).hexdigest()
    cache = sqlite3.connect(work / "models" / f"{identity}.sqlite")
    counts = {"documents": 0, "embeddings": 0, "embedding_cache_hits": 0}
    try:
        cache.execute(
            "CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        with destination.open("w") as output:
            for batch in batched(inputs(), 1024, strict=False):
                values = {}
                missing = {}
                for _, text in batch:
                    if text is None:
                        continue
                    key = hashlib.sha256(text.encode()).hexdigest()
                    cached = cache.execute(
                        "SELECT value FROM vectors WHERE key=?", (key,)
                    ).fetchone()
                    if cached:
                        values[key] = vector(json.loads(cached[0]))
                        counts["embedding_cache_hits"] += 1
                    else:
                        missing[key] = text
                # Similar lengths together reduce padding work in ONNX batches.
                keys = sorted(missing, key=lambda key: len(tokenizer.encode(missing[key]).ids))
                vectors = model.passage_embed([missing[key] for key in keys], batch_size=64)
                for key, embedding in zip(keys, vectors, strict=True):
                    values[key] = vector(embedding)
                    cache.execute(
                        "INSERT OR REPLACE INTO vectors VALUES (?,?)",
                        (key, json.dumps(values[key])),
                    )
                cache.commit()
                for row, text in batch:
                    embedding = values[hashlib.sha256(text.encode()).hexdigest()] if text else None
                    output.write(json.dumps([*row, embedding]) + "\n")
                    counts["documents"] += 1
                    counts["embeddings"] += embedding is not None
                print(
                    f"Prepared {counts['documents']} passages ({counts['embeddings']} embedded)",
                    file=sys.stderr,
                    flush=True,
                )
        destination.replace(spool.with_suffix(".documents"))
    except Error:
        raise
    except Exception as exc:
        raise Error(f"Embedding preparation failed: {exc}") from exc
    finally:
        cache.close()
    return counts


def query_vector(work: Path, pin: dict, query: str):
    model, tokenizer = load_model(str(work), json.dumps(pin, sort_keys=True))
    if len(tokenizer.encode(query).ids) > pin["max_tokens"]:
        raise Error("Query exceeds the indexed model token limit")
    try:
        return vector(next(iter(model.query_embed(query))))
    except Error:
        raise
    except Exception as exc:
        raise Error(f"Query embedding failed: {exc}") from exc
