from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from petgenics.money import free_quantity_value, order_quantity


_ORDER_LINE = re.compile(
    r"^(?P<product>.+?)\s+(?P<quantity>\d+(?:\.\d+)?)\s*\+\s*"
    r"(?P<free_quantity>\d+(?:\.\d+)?)\s*$"
)


def parse_text_order(text: str) -> dict[str, Any]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        raise ValueError(
            "Send the party name first, then one product per line in this format: "
            "Product name 10+2 (10 ordered, 2 free)."
        )

    party_name = lines[0]
    if len(party_name) > 300:
        raise ValueError("Party name must be 300 characters or fewer.")

    items = []
    for line_number, line in enumerate(lines[1:], start=2):
        match = _ORDER_LINE.fullmatch(line)
        if match is None:
            raise ValueError(
                f"Line {line_number} is unclear. Use 'Product name quantity+free', "
                "for example 'Freedom 360 500mg 4+2'. Send one party's order per message."
            )
        product_name = match.group("product").strip()
        if not product_name:
            raise ValueError(f"Line {line_number} is missing the product name.")
        try:
            order_quantity(match.group("quantity"))
            free_quantity_value(match.group("free_quantity"))
        except ValueError as exc:
            raise ValueError(f"Line {line_number}: {exc}") from exc
        items.append({
            "source_text": line,
            "product_query": product_name,
            "quantity": match.group("quantity"),
            "free_quantity": match.group("free_quantity"),
        })
    if len(items) > 20:
        raise ValueError("Send no more than 20 products in one text order.")
    return {"party_name": party_name, "items": items}


def normalize_order_text(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"(?<=\d)(ml|gm|kg|mg)\b", r" \1", value)
    return " ".join(re.findall(r"[a-z0-9]+", value))


def _token_coverage(query: list[str], candidate: list[str]) -> float:
    if not query:
        return 0.0
    matched = sum(
        1
        for token in query
        if any(SequenceMatcher(None, token, other).ratio() >= 0.70 for other in candidate)
    )
    return matched / len(query)


def text_match_score(query: str, candidate: str) -> float:
    normalized_query = normalize_order_text(query)
    normalized_candidate = normalize_order_text(candidate)
    if not normalized_query or not normalized_candidate:
        return 0.0
    query_compact = normalized_query.replace(" ", "")
    candidate_compact = normalized_candidate.replace(" ", "")
    if query_compact == candidate_compact:
        return 1.0
    if len(query_compact) >= 5 and query_compact in candidate_compact:
        return 0.96
    query_tokens = normalized_query.split()
    candidate_tokens = normalized_candidate.split()
    joined_candidates = [
        "".join(candidate_tokens[start:end])
        for start in range(len(candidate_tokens))
        for end in range(start + 1, min(len(candidate_tokens), start + 4) + 1)
    ]
    sequence_score = max(
        SequenceMatcher(None, normalized_query, normalized_candidate).ratio(),
        SequenceMatcher(None, query_compact, candidate_compact).ratio(),
        max(
            (SequenceMatcher(None, query_compact, joined).ratio() for joined in joined_candidates),
            default=0,
        ),
    )
    coverage_score = _token_coverage(query_tokens, candidate_tokens + joined_candidates)
    return 0.65 * coverage_score + 0.35 * sequence_score
