"""Inject a loopback-only fake provider into the real dispatcher CLI for integration tests."""

import json
import os
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from relayforge.adapters.messengers import MessengerError, PublishReceipt
from relayforge.workers.dispatcher import Dispatcher, main


class LocalProvider:
    def send_message(self, token, chat_id, payload):
        assert token == "123456:local-m3-fixture"
        port = int(os.environ["RF_M3_TEST_PORT"])
        data = json.dumps({"chat_id": chat_id, "text": payload["plain_text"]}).encode()
        request = Request(f"http://127.0.0.1:{port}/sendMessage", data,
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=35) as response:
                result = json.load(response)
        except HTTPError as error:
            category = "PERMISSION_DENIED" if error.code == 403 else "TRANSIENT_NETWORK"
            raise MessengerError(category) from None
        return PublishReceipt(str(result["message_id"]))


def checkpoint(name, task):
    if os.environ.get("RF_M3_TEST_POINT") == name:
        print(f"CHECKPOINT {name} {task['claimed_task_id']}", flush=True)
        sys.stdin.readline()


def factory(*args, **kwargs):
    return Dispatcher(*args, **kwargs, connectors={"TELEGRAM": LocalProvider}, hook=checkpoint)


if __name__ == "__main__":
    main(factory)
