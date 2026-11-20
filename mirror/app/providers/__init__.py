from .base import Candidate, Provider, ProviderError
from .google_vision import GoogleVisionProvider
from .serpapi_lens import SerpApiLensProvider
from .tineye import TinEyeProvider

ALL_PROVIDERS: list[Provider] = [GoogleVisionProvider(), TinEyeProvider(), SerpApiLensProvider()]

__all__ = ["ALL_PROVIDERS", "Candidate", "Provider", "ProviderError"]
