"""Transparent English tone heuristics, separate from verified IPO evidence.

This deliberately is not a prediction, investment signal, or population poll.
The small lexicon is auditable, bounded by publisher/community/channel origin,
and returns unknown when available evidence cannot support a summary.
"""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import mean

from .sources.discourse import DiscourseEvidence


METHOD = "english_lexicon_v1; negation window=3; equal origin then equal platform weight; no engagement weighting"
POSITIVE = frozenset({
    "bullish", "optimistic", "promising", "innovative", "excellent", "impressive",
    "profitable", "trusted", "trustworthy", "outperform", "outperforms", "undervalued",
    "successful", "success", "strong", "strength", "exciting", "opportunity", "opportunities",
})
NEGATIVE = frozenset({
    "bearish", "pessimistic", "overvalued", "unprofitable", "fraud", "fraudulent", "scam",
    "disappointing", "weak", "failure", "failed", "failing", "risky", "risk", "risks",
    "controversial", "distrust", "untrustworthy", "dangerous", "overhyped", "skeptical",
    "lawsuit", "loss", "losses",
})
NEGATIONS = frozenset({"not", "no", "never", "neither", "hardly", "without", "isn't", "isnt",
                        "wasn't", "wasnt", "don't", "dont", "doesn't", "doesnt"})
PROMOTION = re.compile(r"\bsponsored\b|\bpaid partnership\b|\baffiliate\b|\bpromo code\b|\bdisclosure\b.*\b(?:own|invest|position)\b", re.I)
HYPE = re.compile(r"\b(?:guaranteed|risk[- ]free|can't lose|cannot lose|to the moon|100x|10x|next trillion)\b", re.I)
RUMOR = re.compile(r"\b(?:rumou?r|unconfirmed|speculat\w*|reportedly|allegedly|might IPO|could IPO)\b", re.I)
TOKENS = re.compile(r"[a-z]+(?:'[a-z]+)?", re.I)


@dataclass(frozen=True)
class EvidenceTone:
    evidence_id: str
    label: str
    score: float | None
    positive_hits: tuple[str, ...]
    negative_hits: tuple[str, ...]
    bias_flags: tuple[str, ...]
    excluded_reason: str | None = None


@dataclass(frozen=True)
class SourceSentiment:
    source_kind: str
    scored_count: int
    independent_origins: int
    score: float


@dataclass(frozen=True)
class SentimentSummary:
    company_name: str
    label: str
    score: float | None
    method: str
    evidence_count: int
    scored_count: int
    independent_origins: int
    excluded_count: int
    positive_count: int
    negative_count: int
    neutral_count: int
    by_source: tuple[SourceSentiment, ...]
    evidence_tones: tuple[EvidenceTone, ...]
    bias_flags: tuple[str, ...]
    limitations: tuple[str, ...]
    generated_at: datetime


def score_evidence(record: DiscourseEvidence, company_name: str) -> EvidenceTone:
    flags = set(record.bias_flags)
    text = record.title + ". " + record.text
    if PROMOTION.search(text):
        flags.add("possible_sponsorship_or_financial_interest")
    if HYPE.search(text):
        flags.add("promotional_or_absolute_language")
    if RUMOR.search(text):
        flags.add("speculation_or_unverified_claim")
    reason = None
    if record.text_kind == "video_metadata" or "metadata_only" in flags:
        reason = "Video metadata cannot establish the speaker's sentiment"
    elif company_name not in record.company_names:
        reason = "Company did not match this evidence"
    elif len(set(record.company_names)) > 1:
        reason = "Multiple watched companies; target-specific tone cannot be reliably attributed"
        flags.add("ambiguous_sentiment_target")
    elif record.language and not record.language.lower().startswith("en"):
        reason = "This lexicon supports English only"
    elif not text.strip() or len(TOKENS.findall(text)) < 8:
        reason = "Insufficient text"
    else:
        letters = [character for character in text if character.isalpha()]
        if letters and sum(character.isascii() for character in letters) / len(letters) < 0.8:
            reason = "Text is not sufficiently English-like for this lexicon"
            flags.add("language_uncertain")
    if reason:
        return EvidenceTone(record.evidence_id, "unknown", None, (), (), tuple(sorted(flags)), reason)

    positive: set[str] = set()
    negative: set[str] = set()
    # Unique lexical hits prevent long transcripts and repeated slogans dominating.
    # Punctuation boundaries prevent negation leaking into the next sentence.
    for sentence in re.split(r"[.!?;\n]+", text):
        tokens = [word.casefold() for word in TOKENS.findall(sentence.replace("’", "'"))]
        for index, token in enumerate(tokens):
            if token not in POSITIVE and token not in NEGATIVE:
                continue
            negated = any(word in NEGATIONS for word in tokens[max(0, index - 3):index])
            is_positive = (token in POSITIVE) != negated
            (positive if is_positive else negative).add(("not " if negated else "") + token)
    hits = len(positive) + len(negative)
    if not hits:
        # No measured opinion is missing information, not a neutral opinion.
        return EvidenceTone(record.evidence_id, "unknown", None, (), (), tuple(sorted(flags)),
                            "No recognized opinion terms; silence is not neutral sentiment")
    score = (len(positive) - len(negative)) / (hits + 2)
    label = "positive" if score > 0.15 else "negative" if score < -0.15 else "neutral"
    return EvidenceTone(record.evidence_id, label, round(score, 4), tuple(sorted(positive)),
                        tuple(sorted(negative)), tuple(sorted(flags)))


def summarize_sentiment(company_name: str, evidence: Iterable[DiscourseEvidence], *,
                        now: datetime | None = None, max_age_days: int = 30) -> SentimentSummary:
    now = now or datetime.now(UTC)
    if now.tzinfo is None or max_age_days < 1:
        raise ValueError("An aware timestamp and positive max_age_days are required")
    selected = [record for record in evidence if company_name in record.company_names]
    flags: set[str] = {"nonrepresentative_sample", "heuristic_sentiment", "engagement_not_truth"}
    tones: list[EvidenceTone] = []
    grouped: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    seen_urls: set[str] = set()
    seen_text: set[str] = set()
    scored: list[EvidenceTone] = []
    for record in selected:
        tone = score_evidence(record, company_name)
        flags.update(tone.bias_flags)
        # Metadata and captions share URLs; excluded metadata must not suppress captions.
        if tone.score is not None:
            normalized = re.sub(r"\W+", " ", record.title + " " + record.text).casefold().strip()
            digest = hashlib.sha256(normalized.encode()).hexdigest()
            published = record.published_at
            retrieved = record.retrieved_at
            reason = None
            if (published is not None and published.tzinfo is None) or retrieved.tzinfo is None:
                reason = "Evidence timestamp lacks timezone"
            elif published and (published < now - timedelta(days=max_age_days) or published > now + timedelta(hours=24)):
                reason = "Publication date is stale or in the future"
                flags.add("stale_or_invalid_publication_date")
            elif retrieved < now - timedelta(days=max_age_days) or retrieved > now + timedelta(hours=24):
                reason = "Retrieval date is stale or in the future"
            elif record.source_url in seen_urls or digest in seen_text:
                reason = "Duplicate URL or identical text; counted once"
                flags.add("duplicates_removed")
            else:
                seen_urls.add(record.source_url)
                seen_text.add(digest)
                if published is None:
                    flags.add("publication_date_unknown")
                grouped[record.source_kind][record.origin_key].append(tone.score)
                scored.append(tone)
            if reason:
                tone = EvidenceTone(tone.evidence_id, "unknown", None, tone.positive_hits,
                                    tone.negative_hits, tone.bias_flags, reason)
        tones.append(tone)

    by_source = tuple(SourceSentiment(kind, sum(len(values) for values in origins.values()),
        len(origins), round(mean(mean(values) for values in origins.values()), 4))
        for kind, origins in sorted(grouped.items()))
    origins_count = sum(len(origins) for origins in grouped.values())
    if origins_count < 3:
        flags.add("few_independent_origins")
    if len(by_source) < 2:
        flags.add("single_platform_or_source_type")
    if any(len(values) > 1 for origins in grouped.values() for values in origins.values()):
        flags.add("origin_weight_capped")
    if any(tone.label == "positive" for tone in scored) and any(tone.label == "negative" for tone in scored):
        flags.add("conflicting_views")
    if len(scored) < 3 or origins_count < 2:
        label, aggregate = "unknown", None
    else:
        aggregate = round(mean(row.score for row in by_source), 4)
        label = "positive" if aggregate > 0.15 else "negative" if aggregate < -0.15 else "neutral"
    limitations = (
        "Observed tone in sampled sources is not representative of public opinion or future IPO performance.",
        "Deterministic English lexicon; sarcasm, quoted opinions, financial jargon and target attribution can be misread.",
        "Each publisher, Reddit community and YouTube channel gets equal weight within its platform; platforms get equal weight.",
        "Origin labels do not certify ownership independence; syndication with different wording may escape deduplication.",
        "At least three scored documents and two independent origins are required; unavailable evidence is unknown.",
        "Promotional flags identify possible bias, not proven misconduct; sentiment never confirms an IPO or contract.",
    )
    return SentimentSummary(company_name, label, aggregate, METHOD, len(selected), len(scored),
        origins_count, len(selected) - len(scored), sum(tone.label == "positive" for tone in scored),
        sum(tone.label == "negative" for tone in scored), sum(tone.label == "neutral" for tone in scored),
        by_source, tuple(tones), tuple(sorted(flags)), limitations, now)
