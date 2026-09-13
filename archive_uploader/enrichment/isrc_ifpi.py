"""IFPI ISRC Search provider (https://isrcsearch.ifpi.org/).

STATUS: link-only, not a real fetch() yet — read this before wiring it
into DEFAULT_PROVIDERS.

isrcsearch.ifpi.org is a client-side single-page app (it's the IFPI
front end for the same SoundExchange ISRC database also exposed at
isrc.soundexchange.com — ~20M recordings). It has no documented public
API, and nothing came up searching for one. Guessing a query endpoint
felt worse than being upfront: a wrong URL would either 404 loudly or,
worse, silently return nothing and look like "no match" for every
release.

What this file does instead: if a release already has an ISRC (from
local tags, or from an earlier provider in DEFAULT_PROVIDERS — note
overrides apply *after* every provider runs, per pipeline.py, so an
ISRC that only exists in your .archive_meta.json won't reach this
fetch() call), it adds a badge linking to the ISRC Search homepage so
you can paste the code in and check the result by hand.

To turn this into a real fetch()-based provider (same shape as
discogs.py / lastfm.py):
  1. Open https://isrcsearch.ifpi.org in a browser with DevTools open
     (Network tab, filtered to XHR/Fetch).
  2. Run a search there — by ISRC, or by artist/title.
  3. Copy the request URL + payload it sends, and the shape of the
     JSON it returns.
  4. Send that over (or drop it in here) and this gets the same
     requests.get/post + JSON-parse treatment as the other providers.
"""
from __future__ import annotations

from typing import Optional

from ..models import Release
from .base import Provider

SEARCH_HOMEPAGE = "https://isrcsearch.ifpi.org/"


class IsrcIfpiProvider(Provider):
    name = "IFPI ISRC Search"
    logo_url = "https://www.google.com/s2/favicons?domain=ifpi.org&sz=32"

    def fetch(self, rel: Release) -> Optional[dict]:
        if not rel.isrc:
            return None
        # No specific-record deep-link URL scheme is known either (same
        # reason as above) — this can only point at the search homepage,
        # not the matched record, until the real endpoint is captured.
        return {"url": SEARCH_HOMEPAGE}
