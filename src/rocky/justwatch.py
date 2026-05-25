from __future__ import annotations

import logging
from typing import Any, Optional

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type, before_sleep_log

from rocky.db import AvailabilityOffer

logger = logging.getLogger("rocky.justwatch")

JUSTWATCH_GRAPHQL_URL = "https://apis.justwatch.com/graphql"

_SEARCH_QUERY = """
query GetSearchTitles(
  $searchTitlesFilter: TitleFilter!,
  $country: Country!,
  $language: Language!,
  $first: Int!,
  $filter: OfferFilter!,
  $offset: Int = 0
) {
  popularTitles(
    country: $country
    filter: $searchTitlesFilter
    first: $first
    sortBy: POPULAR
    sortRandomSeed: 0
    offset: $offset
  ) {
    edges {
      node {
        objectType
        content(country: $country, language: $language) {
          title
          originalReleaseYear
        }
        offers(country: $country, platform: WEB, filter: $filter) {
          monetizationType
          standardWebURL
          package {
            clearName
            technicalName
            shortName
          }
        }
      }
    }
  }
}
"""


class JustWatchClient:
    def __init__(
        self,
        country: str = "IN",
        language: str = "en",
        max_results: int = 3,
        best_only: bool = True,
    ):
        self.country = country.upper()
        self.language = language
        self.max_results = max_results
        self.best_only = best_only
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0"})

    def _request_payload(self, title: str, year: Optional[int]) -> dict[str, Any]:
        search_filter: dict[str, Any] = {
            "searchQuery": title,
            "includeTitlesWithoutUrl": True,
            "objectTypes": ["MOVIE"],
        }
        if year:
            search_filter["releaseYear"] = {"min": year, "max": year}

        return {
            "operationName": "GetSearchTitles",
            "variables": {
                "first": self.max_results,
                "searchTitlesFilter": search_filter,
                "language": self.language,
                "country": self.country,
                "filter": {"bestOnly": self.best_only},
                "offset": 0,
            },
            "query": _SEARCH_QUERY,
        }

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((requests.exceptions.ConnectionError, requests.exceptions.Timeout)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def _fetch_nodes(self, title: str, year: Optional[int]) -> list[dict[str, Any]]:
        payload = self._request_payload(title=title, year=year)
        response = self.session.post(JUSTWATCH_GRAPHQL_URL, json=payload, timeout=40)
        response.raise_for_status()
        body = response.json()
        if "errors" in body:
            raise ValueError(f"JustWatch API errors for title={title!r}: {body['errors'][:1]}")
        edges = body.get("data", {}).get("popularTitles", {}).get("edges", [])
        return [edge.get("node", {}) for edge in edges if edge.get("node")]

    @staticmethod
    def _normalize(value: str) -> str:
        return "".join(ch for ch in value.lower().strip() if ch.isalnum() or ch.isspace())

    def _pick_best_node(
        self,
        title: str,
        year: Optional[int],
        nodes: list[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        if not nodes:
            return None

        normalized_title = self._normalize(title)

        def score(node: dict[str, Any]) -> tuple[int, int]:
            content = node.get("content") or {}
            candidate_title = self._normalize(content.get("title") or "")
            candidate_year = content.get("originalReleaseYear")
            title_score = 2 if candidate_title == normalized_title else 1 if normalized_title in candidate_title else 0
            if year is None:
                year_score = 1
            else:
                year_score = 2 if candidate_year == year else 0
            return (title_score, year_score)

        ranked = sorted(nodes, key=score, reverse=True)
        return ranked[0]

    def lookup_movie_availability(self, title: str, year: Optional[int]) -> list[AvailabilityOffer]:
        nodes = self._fetch_nodes(title=title, year=year)
        best = self._pick_best_node(title=title, year=year, nodes=nodes)
        if not best:
            return []

        offers = best.get("offers") or []
        normalized: list[AvailabilityOffer] = []
        seen: set[tuple[str, Optional[str], str, Optional[str]]] = set()

        for offer in offers:
            package = offer.get("package") or {}
            provider_name = package.get("clearName") or package.get("technicalName") or "Unknown"
            provider_code = package.get("shortName") or package.get("technicalName")
            monetization_type = (offer.get("monetizationType") or "").upper()
            url = offer.get("standardWebURL")
            if not monetization_type:
                continue

            key = (provider_name, provider_code, monetization_type, url)
            if key in seen:
                continue
            seen.add(key)
            normalized.append(
                AvailabilityOffer(
                    provider_name=provider_name,
                    provider_code=provider_code,
                    monetization_type=monetization_type,
                    url=url,
                )
            )

        return normalized
