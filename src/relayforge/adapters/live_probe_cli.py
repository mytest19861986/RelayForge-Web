"""Owner opt-in capability check for explicitly selected messenger credentials."""

import argparse
import json
import os
from pathlib import Path

from relayforge.adapters.messengers import CONNECTORS, MessengerError
from relayforge.adapters.net_guard import OutboundTransportError
from relayforge.core.rich_text import CanonicalRichText, compile_rich_text
from relayforge.core.security import scrub_sensitive_text

PLATFORMS = {
    "telegram": "TELEGRAM",
    "bale": "BALE",
    "eitaa": "EITAA",
    "rubika": "RUBIKA_EXPERIMENTAL",
    "soroush_plus": "SOROUSH_EXPERIMENTAL",
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("platform", choices=PLATFORMS)
    parser.add_argument("--confirm-live-probe", action="store_true")
    parser.add_argument("--send-test-post", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("tmp/at22-live-probe-report.json"))
    args = parser.parse_args(argv)
    if not args.confirm_live_probe:
        parser.error("--confirm-live-probe is required")
    prefix = "RF_AT22_" + args.platform.upper()
    token = os.environ.get(prefix + "_TOKEN")
    chat = os.environ.get(prefix + "_CHAT")
    if not token or (args.send_test_post and not chat):
        parser.error("selected platform requires its token and an explicit chat for publication")
    platform = PLATFORMS[args.platform]
    adapter = CONNECTORS[platform]()
    report = {"platform": args.platform, "maturity": adapter.platform_tier, "published": False}
    try:
        report["connection"] = adapter.test_connection(token)["status"]
        if args.send_test_post:
            payload = compile_rich_text(CanonicalRichText("آزمون اتصال رله‌فورج"), platform)
            adapter.test_publish(token, chat, payload, explicit_user_confirmation=True)
            report["published"] = True
    except (MessengerError, OutboundTransportError, ValueError, KeyError):
        report["error"] = "CAPABILITY_PROBE_FAILED"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(scrub_sensitive_text(json.dumps(report, ensure_ascii=False), (token,)))
    return int("error" in report)


if __name__ == "__main__":
    raise SystemExit(main())
