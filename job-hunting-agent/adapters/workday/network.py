from __future__ import annotations

from urllib.parse import urlparse


DENIED_DOMAINS = ("boeing.com", "myworkdayjobs.com", "workday.com")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
LOCAL_SCHEMES = {"about", "data", "file", "blob"}


def is_denied_domain(host: str) -> bool:
    host = (host or "").lower().strip(".")
    return any(host == domain or host.endswith(f".{domain}") for domain in DENIED_DOMAINS)


def is_allowed_replay_url(url: str) -> bool:
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme in LOCAL_SCHEMES:
        return True
    if scheme in {"http", "https"}:
        return host in LOCAL_HOSTS
    return True


def install_network_guard(context, metrics: dict | None = None):
    metrics = metrics if metrics is not None else {}
    metrics.setdefault("external_requests", 0)
    metrics.setdefault("blocked_urls", [])

    def guard(route):
        request = route.request
        url = request.url
        host = (urlparse(url).hostname or "").lower()
        if is_denied_domain(host) or not is_allowed_replay_url(url):
            metrics["external_requests"] += 1
            metrics["blocked_urls"].append(url)
            route.abort()
            return
        route.continue_()

    context.route("**/*", guard)
    return metrics
