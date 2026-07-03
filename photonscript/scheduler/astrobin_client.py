"""AstroBin API client — fetch top images and estimate exposure requirements."""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from photonscript.shared.models import CelestialTarget

logger = logging.getLogger(__name__)

ASTROBIN_API_BASE = "https://www.astrobin.com/api/v1"


class AstroBinClient:
    """Query AstroBin for reference images and exposure metadata.

    Uses AstroBin's REST API to find the best-rated images of a target
    and extract exposure information to guide our imaging plans.
    """

    def __init__(self, api_key: str = "", api_secret: str = ""):
        self.api_key = api_key
        self.api_secret = api_secret
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=ASTROBIN_API_BASE,
                timeout=30.0,
                params={"api_key": self.api_key, "api_secret": self.api_secret, "format": "json"},
            )
        return self._client

    async def search_images(
        self,
        target_name: str,
        limit: int = 10,
    ) -> list[dict]:
        """Search AstroBin for images of a target, sorted by rating."""
        if not self.api_key:
            logger.warning("AstroBin API key not configured — using offline catalog only")
            return []

        client = await self._get_client()
        try:
            resp = await client.get(
                "/image/",
                params={
                    "subjects": target_name,
                    "order_by": "-likes",
                    "limit": limit,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("objects", [])
        except Exception as e:
            logger.warning("AstroBin search failed for '%s': %s", target_name, e)
            return []

    async def get_image_details(self, image_id: str) -> Optional[dict]:
        """Get detailed info for a specific AstroBin image."""
        if not self.api_key:
            return None
        client = await self._get_client()
        try:
            resp = await client.get(f"/image/{image_id}/")
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning("AstroBin image detail fetch failed: %s", e)
            return None

    async def estimate_exposures_for_target(
        self,
        target_name: str,
    ) -> dict:
        """Look at top AstroBin images and estimate recommended exposure times.

        Returns a summary of what the best imagers are using for this target.
        """
        images = await self.search_images(target_name, limit=20)
        if not images:
            return {"source": "default", "note": "No AstroBin data; using defaults"}

        # Collect exposure data from image descriptions and metadata
        exposure_data = {
            "total_images_surveyed": len(images),
            "source": "astrobin",
            "filters_seen": set(),
            "avg_integration_hours": 0,
            "recommendations": [],
        }

        total_integration = 0
        count = 0
        for img in images:
            # AstroBin stores integration time in various fields
            integration = img.get("integration")
            if integration:
                total_integration += integration
                count += 1

        if count > 0:
            exposure_data["avg_integration_hours"] = round(total_integration / count / 3600, 1)

        return exposure_data

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# ---------------------------------------------------------------------------
# Community filter-mix suggestion: average real acquisition ratios for a target
# ---------------------------------------------------------------------------

import json as _json
import re as _re
from pathlib import Path as _Path

# Map real-world filter names to our filter classes
_FILTER_PATTERNS = [
    (r"h[\s\-_]?a(lpha)?|ha\b|hα", "Ha"),
    (r"o[\s\-_]?iii|o3\b|oxygen", "OIII"),
    (r"s[\s\-_]?ii|s2\b|sulfur|sulphur", "SII"),
    (r"^l$|lum|luminance|clear|uv[\s/]?ir", "L"),
    (r"^r$|red", "R"),
    (r"^g$|green", "G"),
    (r"^b$|blue", "B"),
]


def classify_filter(name: str) -> str | None:
    """Map an arbitrary filter name ('Astrodon Ha 5nm', 'Baader Red') to a class."""
    n = (name or "").strip().lower()
    if not n:
        return None
    for pattern, cls in _FILTER_PATTERNS:
        if _re.search(pattern, n):
            return cls
    return None


def extract_acquisition_hours(image: dict) -> dict[str, float]:
    """Pull per-filter-class hours from one image's acquisition data.

    AstroBin API responses vary by version; we look for any list field that
    holds acquisition-like entries (filter + number + duration).
    """
    hours: dict[str, float] = {}
    candidates = []
    for key in ("deep_sky_acquisitions", "deepSkyAcquisitions", "acquisitions"):
        val = image.get(key)
        if isinstance(val, list):
            candidates = val
            break
    for acq in candidates:
        if not isinstance(acq, dict):
            continue
        fname = (acq.get("filter") or acq.get("filter_name")
                 or acq.get("filter2Name") or acq.get("filter_make") or "")
        if isinstance(fname, dict):
            fname = fname.get("name", "")
        cls = classify_filter(str(fname))
        if cls is None:
            continue
        try:
            number = float(acq.get("number") or 0)
            duration = float(acq.get("duration") or 0)
        except (TypeError, ValueError):
            continue
        if number > 0 and duration > 0:
            hours[cls] = hours.get(cls, 0.0) + number * duration / 3600
    return hours


def aggregate_mix(images: list[dict]) -> dict:
    """Average filter mix across all images that carry acquisition data."""
    totals: dict[str, float] = {}
    with_data = 0
    for img in images:
        h = extract_acquisition_hours(img)
        if not h:
            continue
        with_data += 1
        for cls, val in h.items():
            totals[cls] = totals.get(cls, 0.0) + val

    grand = sum(totals.values())
    mix = ({cls: round(v / grand * 100, 1) for cls, v in
            sorted(totals.items(), key=lambda kv: -kv[1])} if grand else {})
    return {
        "mix": mix,
        "images_sampled": len(images),
        "images_with_data": with_data,
        "total_community_hours": round(grand, 1),
    }


class AstroBinMixSuggester:
    """Fetch + cache community filter mixes per target."""

    def __init__(self, config):
        self.config = config
        self.cache_path = _Path(config.data_dir) / "astrobin_cache.json"
        self.cache: dict = {}
        if self.cache_path.exists():
            try:
                self.cache = _json.loads(self.cache_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass

    async def suggest(self, target_name: str, catalog_id: str = "") -> dict:
        key = (catalog_id or target_name).lower()
        if key in self.cache:
            return {**self.cache[key], "cached": True}

        if not self.config.astrobin_api_key:
            return {"error": "AstroBin API key not set — request one at "
                             "astrobin.com/api/request-key and add it in "
                             "System & Config."}

        client = AstroBinClient(self.config.astrobin_api_key,
                                self.config.astrobin_api_secret)
        # Prefer catalog designation (how AstroBin indexes subjects), then name
        images: list[dict] = []
        for subject in filter(None, [catalog_id.replace(" ", ""),
                                     catalog_id, target_name]):
            images = await client.search_images(subject, limit=100)
            if images:
                break
        if not images:
            return {"error": f"No AstroBin images found for "
                             f"'{catalog_id or target_name}'."}

        # Log field shape once — helps adapt if the API schema differs
        logger.info("AstroBin sample image fields: %s",
                    sorted(images[0].keys())[:30])

        result = aggregate_mix(images)
        if not result["mix"]:
            result["error"] = (f"Found {len(images)} images but none exposed "
                               "acquisition details via the API.")
        else:
            self.cache[key] = result
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(_json.dumps(self.cache, indent=1),
                                       encoding="utf-8")
        return result
