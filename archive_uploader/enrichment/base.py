"""
One class per external service. Adding a new provider (Discogs,
Bandcamp, whatever) means writing one file here — payload.py's
description/badge rendering and pipeline.py's merge loop never change,
because they iterate whatever providers are registered.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..models import Release


class Provider(ABC):
    name: str = "provider"
    logo_url: str = ""

    @abstractmethod
    def fetch(self, rel: Release) -> Optional[dict]:
        """
        Return a dict of fields to merge into the Release. Three keys are
        special and drive the generic badge/external-identifier
        rendering in ia/payload.py:
          - "id":    this service's own id for the release
          - "url":   a link to the release's page on that service
          - "links": optional list of extra links this provider turned up
                      about the release (e.g. MusicBrainz's url-rels —
                      Discogs, Bandcamp, YouTube, etc. relations attached
                      to the MB entry). Each item is a dict:
                      {"url": "...", "service": "" , "logo_url": ""}.
                      "service"/"logo_url" may be left blank — payload.py
                      infers a service name and favicon from the domain
                      when they're empty.
        Any other recognized release key (genre, label, country,
        catalog_number, release_type, release_status, format, styles,
        secondary_artists, upc, external_description, copyright,
        audio_spec, cover_url, wikipedia_article) is merged in directly.
        A provider may also return ``tracks`` as a list of dictionaries with
        ``position``, ``title``, ``artist``, and ``composer``. Return None on no match.
        """
        raise NotImplementedError


# Alias for backwards compatibility
BaseEnricher = Provider
