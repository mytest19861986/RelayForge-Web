import ipaddress
import json
import logging
import socket
import traceback

import httpx

from relayforge.core.security import scrub_sensitive_text

TRANSITION_NETWORKS = tuple(ipaddress.IPv6Network(network) for network in (
    "64:ff9b::/96", "64:ff9b:1::/48", "2001::/32", "2002::/16",
))


class OutboundTransportError(RuntimeError):
    """A safe-to-report outbound request failure."""


class SSRFBlockedError(OutboundTransportError):
    """The outbound target violates the network policy."""


def validate_public_ip(value: str) -> None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise SSRFBlockedError("Invalid destination address") from None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if (
        not address.is_global
        or address.is_multicast
        or address.is_reserved
        or (
            isinstance(address, ipaddress.IPv6Address)
            and (address.is_site_local or any(address in net for net in TRANSITION_NETWORKS))
        )
        or "%" in value
    ):
        raise SSRFBlockedError("Non-public destination address")


class SecretScrubbingTransport(httpx.BaseTransport):
    def __init__(self, transport, *, extra_secrets=()):
        self._transport = transport
        self._secrets = tuple(extra_secrets)

    def handle_request(self, request):
        try:
            return self._transport.handle_request(request)
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            message = scrub_sensitive_text(str(exc), self._secrets)
        raise OutboundTransportError(message)

    def close(self):
        self._transport.close()


class _PinnedTransport(httpx.BaseTransport):
    def __init__(self, transport):
        self._transport = transport

    def handle_request(self, request):
        url = request.url
        if url.scheme != "https" or not url.host or url.userinfo or "%" in url.host:
            raise SSRFBlockedError("Expected an HTTPS URL without user information")
        try:
            addresses = socket.getaddrinfo(url.raw_host.decode(), url.port or 443,
                                           type=socket.SOCK_STREAM)
        except OSError:
            raise OutboundTransportError("DNS resolution failed") from None
        if not addresses:
            raise SSRFBlockedError("No destination addresses")
        for address in addresses:
            validate_public_ip(address[4][0])
        headers = request.headers.copy()
        headers["Host"] = url.netloc.decode()
        return self._transport.handle_request(httpx.Request(
            request.method, url.copy_with(host=addresses[0][4][0]), headers=headers,
            stream=request.stream,
            extensions={**request.extensions, "sni_hostname": url.raw_host.decode()},
        ))

    def close(self):
        self._transport.close()


class SafeDialer:
    def __init__(self, *, transport=None, extra_secrets=()):
        self._secrets = tuple(extra_secrets)
        self._transport = SecretScrubbingTransport(
            _PinnedTransport(transport if transport is not None else httpx.HTTPTransport(
                trust_env=False,
                # Pooling by pinned IP would otherwise reuse TLS across logical hosts.
                limits=httpx.Limits(max_keepalive_connections=0),
            )), extra_secrets=self._secrets,
        )

    def request(
        self, method, url, *, headers=None, content=None, allow_error_response=False,
        follow_redirects=True, timeout_sec=5.0, max_response_bytes=None,
    ):
        with self._transport:
            try:
                for hop in range(4):
                    request_headers = httpx.Headers(headers)
                    if max_response_bytes is not None:
                        request_headers["Accept-Encoding"] = "identity"
                    request = httpx.Request(
                        method, url, headers=request_headers, content=content,
                        extensions={"timeout": dict.fromkeys(
                            ("connect", "read", "write", "pool"), timeout_sec
                        )},
                    )
                    response = self._transport.handle_request(request)
                    try:
                        response.request = request
                        if max_response_bytes is None:
                            response.read()
                        else:
                            if response.headers.get("Content-Encoding", "identity") != "identity":
                                raise OutboundTransportError("Unexpected encoded response")
                            body = bytearray()
                            chunks = (response.content,) if response.is_stream_consumed else (
                                response.iter_raw(65536)
                            )
                            for chunk in chunks:
                                if len(body) + len(chunk) > max_response_bytes:
                                    raise OutboundTransportError("Response size limit exceeded")
                                body.extend(chunk)
                            bounded = httpx.Response(response.status_code,
                                                     headers=response.headers,
                                                     content=bytes(body), request=request)
                    finally:
                        response.close()
                    if max_response_bytes is not None:
                        response = bounded
                    if response.status_code not in {301, 302, 303, 307, 308}:
                        if not allow_error_response:
                            response.raise_for_status()
                        return response
                    if not follow_redirects or hop == 3 or "location" not in response.headers:
                        raise SSRFBlockedError("Invalid redirect or redirect limit exceeded")
                    next_url = request.url.join(response.headers["location"])
                    if response.status_code == 303 or (
                        response.status_code in {301, 302} and method.upper() == "POST"
                    ):
                        method, content, headers = "GET", None, None
                    if (request.url.scheme, request.url.netloc) != (
                        next_url.scheme, next_url.netloc
                    ):
                        if content is not None:
                            raise SSRFBlockedError("Cross-origin redirect cannot forward a body")
                        headers = None
                    url = next_url
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                message = scrub_sensitive_text(str(exc), self._secrets)
            raise OutboundTransportError(message)


class SecretLogFilter(logging.Filter):
    def __init__(self, *, extra_secrets=()):
        super().__init__()
        self._secrets = tuple(extra_secrets)

    def filter(self, record):
        record.msg = scrub_sensitive_text(record.getMessage(), self._secrets)
        record.args = ()
        if record.exc_info:
            record.exc_text = "".join(traceback.format_exception(*record.exc_info))
            record.exc_info = None
        if record.exc_text:
            record.exc_text = scrub_sensitive_text(record.exc_text, self._secrets)
        if record.stack_info:
            record.stack_info = scrub_sensitive_text(record.stack_info, self._secrets)
        return True


class JsonLogFormatter(logging.Formatter):
    def format(self, record):
        event = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_text:
            event["exception"] = record.exc_text
        if record.stack_info:
            event["stack"] = record.stack_info
        return json.dumps(event, ensure_ascii=False)
