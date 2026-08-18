"""Shared native HTTP/SOCKS proxy helpers."""

from urllib.parse import urlparse

from telegram.request import HTTPXRequest

SUPPORTED_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}


def validate_proxy_url(proxy_url: str, service: str = "Proxy") -> str:
    """Return a normalized proxy URL or raise ValueError."""
    proxy = str(proxy_url or "").strip()
    if not proxy:
        return ""
    parsed = urlparse(proxy)
    if parsed.scheme not in SUPPORTED_PROXY_SCHEMES:
        raise ValueError(
            f"{service} proxy scheme must be http, https, socks5, or socks5h"
        )
    if not parsed.hostname:
        raise ValueError(f"{service} proxy URL must include a host")
    return proxy


def build_telegram_request(proxy_url: str = "") -> HTTPXRequest:
    """Build a PTB request with explicit direct/proxy networking.

    ``trust_env=False`` makes the Constance option authoritative instead of
    silently inheriting HTTP_PROXY/HTTPS_PROXY from the service environment.
    """
    proxy = validate_proxy_url(proxy_url, service="Telegram")
    return HTTPXRequest(
        proxy=proxy or None,
        httpx_kwargs={"trust_env": False},
    )
