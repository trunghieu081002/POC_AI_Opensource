#!/usr/bin/env python3
"""A minimal AWS SigV4 S3 client, stdlib only - no boto3, no `mc`, no `aws`
CLI, and not `curl --aws-sigv4` (that flag needs curl 7.75+; this pack
targets both families and a plain Debian/Ubuntu host at an older curl would
not have it - a real portability gap found while writing this suite, not
guessed). Every host this suite runs on already has python3 (base/
python-modern), so this has no new dependency to install.

Usage: s3sig.py METHOD BUCKET [KEY] [--body TEXT | --body-file PATH]
Env (matches this pack's own params): S3_HOST, S3_PORT, S3_ACCESS_KEY,
S3_SECRET_KEY.

Prints the response body to stdout on 2xx. On a non-2xx, prints the AWS
error <Code> to stderr and exits 1 - what the negative check greps for.

No type hints, no f-string-adjacent 3.7+ syntax beyond f-strings themselves
(3.6+) - found the hard way: the system `python3` a `sudo dpagent test`
actually runs this under is whatever the OS ships, not this repo's own
venv. On Oracle Linux 8 (and RHEL8 generally) that is Python 3.6.8, which
does not have `from __future__ import annotations` (3.7+) at all - a first
draft of this file used it and failed with "SyntaxError: future feature
annotations is not defined" the first time it actually ran under `sudo`.
"""
import hashlib
import hmac
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

REGION = "us-east-1"
SERVICE = "s3"


def _sign(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def _signing_key(secret, date_stamp):
    k_date = _sign(("AWS4" + secret).encode(), date_stamp)
    k_region = _sign(k_date, REGION)
    k_service = _sign(k_region, SERVICE)
    return _sign(k_service, "aws4_request")


def request(method, path, body=b""):
    host = os.environ.get("S3_HOST", "127.0.0.1")
    port = os.environ.get("S3_PORT", "9000")
    access_key = os.environ["S3_ACCESS_KEY"]
    secret_key = os.environ["S3_SECRET_KEY"]

    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()

    host_header = f"{host}:{port}"
    canonical_headers = (
        f"host:{host_header}\n"
        f"x-amz-content-sha256:{payload_hash}\n"
        f"x-amz-date:{amz_date}\n"
    )
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    canonical_request = "\n".join([
        method, path, "", canonical_headers, signed_headers, payload_hash,
    ])
    scope = f"{date_stamp}/{REGION}/{SERVICE}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])
    signature = hmac.new(_signing_key(secret_key, date_stamp), string_to_sign.encode(),
                         hashlib.sha256).hexdigest()
    auth = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    req = urllib.request.Request(
        f"http://{host_header}{path}", data=body if body else None, method=method,
        headers={
            "Host": host_header,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
            "Authorization": auth,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        print("usage: s3sig.py METHOD BUCKET [KEY] [--body TEXT | --body-file PATH]",
              file=sys.stderr)
        return 2
    method, bucket = args[0], args[1]
    rest = args[2:]
    key = ""
    body = b""
    i = 0
    while i < len(rest):
        if rest[i] == "--body":
            body = rest[i + 1].encode()
            i += 2
        elif rest[i] == "--body-file":
            with open(rest[i + 1], "rb") as f:
                body = f.read()
            i += 2
        else:
            key = rest[i]
            i += 1
    path = f"/{bucket}" + (f"/{key}" if key else "/")

    status, resp_body = request(method, path, body)
    if status // 100 != 2:
        code = ""
        if b"<Code>" in resp_body:
            code = resp_body.split(b"<Code>")[1].split(b"</Code>")[0].decode()
        print(f"HTTP {status} {code}".strip(), file=sys.stderr)
        sys.stderr.buffer.write(resp_body)
        return 1
    sys.stdout.buffer.write(resp_body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
