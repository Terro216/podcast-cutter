"""Dense retrieval: the words a listener uses are not the words that were said.

The baskets put a number on this before any of it was built: the `meaning`
class — «где рассказывают про эффект пустышки» against an episode that only
ever says «плацебо» — scored **0% in all four runs**. Lexical search cannot
cross that gap by definition, so windows are embedded once at index time and
a query can match on meaning as well as on tokens.

The model is `multilingual-e5-small` on CTranslate2 int8, chosen by the
research pass in `ROADMAP.md` §12: one multilingual model for both baskets,
running on the same engine faster-whisper already ships, so the image grows by
zero dependencies — only the converted weights on the volume. Similarity is
exact NumPy over a few hundred vectors per episode; the same research rejected
ANN indexes at this scale.

Optional exactly the way recognition is: no model directory, no import, no
vectors — and search degrades to the lexical behaviour that produced the
committed baselines, rather than to an error.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: Stored on every transcript whose windows were embedded, so vectors from an
#: older or different model are recognisable instead of silently comparable.
#: The lemma index taught this the hard way — a version that is written but
#: never checked is a version that does not exist.
EMBEDDING_MODEL = "multilingual-e5-small-ct2-int8"

EMBEDDING_DIM = 384

#: A dense hit must clear BOTH bars below, or it is not an answer — it is
#: merely the nearest thing lying around, and returning it would turn every
#: negative query into a confident lie, the one failure the baskets punish
#: hardest. Both were measured over all 208 basket queries (2 languages ×
#: 2 variants), not guessed:
#:
#: * the strongest *absolute* similarity any negative reaches is 0.842
#:   («brain» against the biomimicry episode), so the absolute floor alone
#:   would sit a razor's 0.003 under it;
#: * the strongest *relative* margin over the episode's median any negative
#:   reaches is 0.0545 («подруга» against lora-spies — a word the episode
#:   never says about a conversation it half has), so the margin alone is a
#:   razor too;
#: * no negative clears both at once, which is the whole point of requiring
#:   the conjunction: a real answer is close to the query *and* sticks out
#:   of its episode's background, and e5-small's compressed score band makes
#:   either property alone unreliable.
MIN_SIMILARITY = 0.84
MIN_MARGIN = 0.05

#: e5 was trained with these prefixes; without them scores drop measurably.
_QUERY_PREFIX = "query: "
_PASSAGE_PREFIX = "passage: "

_MAX_TOKENS = 512
_BATCH = 32


class Embedder:
    """Sentence vectors from the CTranslate2 encoder, loaded on first use.

    Loading takes a moment and the bot must start without the model being
    touched at all, so everything heavy happens behind :meth:`_load`.
    """

    def __init__(self, model_dir: Path, threads: int = 4) -> None:
        self.model_dir = Path(model_dir)
        self.threads = threads
        self._encoder = None
        self._tokenizer = None

    def _load(self) -> None:
        if self._encoder is not None:
            return
        import ctranslate2
        from tokenizers import Tokenizer

        self._tokenizer = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))
        self._encoder = ctranslate2.Encoder(
            str(self.model_dir),
            device="cpu",
            compute_type="int8",
            intra_threads=self.threads,
        )
        logger.info("Embedder ready: %s from %s", EMBEDDING_MODEL, self.model_dir)

    def _vectors(self, texts: list[str]):
        """Mean-pooled, L2-normalised vectors, one row per text."""
        import numpy as np

        self._load()
        rows = []
        for offset in range(0, len(texts), _BATCH):
            batch = texts[offset : offset + _BATCH]
            encoded = self._tokenizer.encode_batch(batch)
            tokens = [e.tokens[:_MAX_TOKENS] for e in encoded]
            output = self._encoder.forward_batch(tokens)
            hidden = np.array(output.last_hidden_state)  # [batch, time, dim]
            for index, sequence in enumerate(tokens):
                length = len(sequence)
                pooled = hidden[index, :length].mean(axis=0)
                rows.append(pooled / (np.linalg.norm(pooled) or 1.0))
        return np.array(rows, dtype=np.float32)

    def encode_passages(self, texts: list[str], on_progress=None) -> list[bytes]:
        """Window vectors as raw bytes, ready for a BLOB column.

        ``on_progress`` is called with the count of texts embedded so far,
        from whatever thread is doing the work — the same contract the
        recogniser's ``on_segment`` keeps, and for the same reason: a stage
        with no bar looks like a hang the moment the episode is long.
        """
        if not texts:
            return []
        prefixed = [_PASSAGE_PREFIX + text for text in texts]
        blobs: list[bytes] = []
        for offset in range(0, len(prefixed), _BATCH):
            batch = self._vectors(prefixed[offset : offset + _BATCH])
            blobs.extend(vector.tobytes() for vector in batch)
            if on_progress is not None:
                on_progress(len(blobs))
        return blobs

    def rank(self, query: str, blobs: list[bytes]) -> list[tuple[int, float]]:
        """Indices into ``blobs`` by similarity to the query, best first.

        Every index is returned — the caller applies :data:`MIN_SIMILARITY`,
        because the threshold is retrieval policy, not vector arithmetic, and
        the tests that pin the policy should not need real vectors to do it.
        """
        import numpy as np

        if not blobs:
            return []
        matrix = np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(
            len(blobs), -1
        )
        query_vector = self._vectors([_QUERY_PREFIX + query])[0]
        similarities = matrix @ query_vector
        order = np.argsort(-similarities)
        return [(int(i), float(similarities[i])) for i in order]


def build_embedder(settings) -> Embedder | None:
    """The embedder the configuration asks for, or honestly none.

    Mirrors ``build_indexer``: a missing model directory or a missing library
    is a feature that is off, not a bot that will not start.
    """
    if not settings.embed_model_dir:
        return None
    model_dir = Path(settings.embed_model_dir)
    if not (model_dir / "model.bin").exists():
        logger.warning(
            "EMBED_MODEL_DIR=%s has no model.bin — dense search stays off.",
            model_dir,
        )
        return None
    try:
        import ctranslate2  # noqa: F401
        import numpy  # noqa: F401
        from tokenizers import Tokenizer  # noqa: F401
    except ImportError as exc:  # pragma: no cover - depends on the environment
        logger.warning("Dense search off: %s", exc)
        return None
    return Embedder(model_dir, threads=settings.asr_threads)
