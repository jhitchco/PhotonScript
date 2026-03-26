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
