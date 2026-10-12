"""A small JSON-over-HTTP helper with retries. Standard library only."""

import json
import time
import urllib.error
import urllib.request

RETRY_STATUSES = (429, 500, 502, 503, 529)


def post_json(url, body, headers, retries=6, timeout=60):
    """POST `body` as JSON and return the decoded response.

    Retries rate limits, overload and transient server errors with exponential backoff
    (1, 2, 4, ... seconds, capped at 30). Never echoes an error response body: an error page can
    carry request context, and request context here is ticket text.
    """
    data = json.dumps(body).encode()
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in RETRY_STATUSES and attempt < retries - 1:
                time.sleep(min(2 ** attempt, 30))
                continue
            raise RuntimeError("{} HTTP {}".format(url, e.code)) from None
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries - 1:
                time.sleep(min(2 ** attempt, 30))
                continue
            raise
