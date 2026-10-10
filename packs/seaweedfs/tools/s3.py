#!/usr/bin/env python3
"""A tiny, stdlib-only S3 client (AWS Signature V4, path-style) for the
seaweedfs pack's own steps, verify and acceptance suite.

Why not `curl --aws-sigv4`: that needs curl >= 7.75 and RHEL/Oracle Linux 8
ship 7.61. Why not boto3/awscli: the pack must not depend on a Python
environment it does not own. Reads S3_ENDPOINT / S3_ACCESS_KEY / S3_SECRET_KEY
from the environment.

    s3.py list-buckets
    s3.py create-bucket BUCKET            (an already-existing bucket is fine)
    s3.py head-bucket BUCKET
    s3.py put BUCKET KEY FILE
    s3.py get BUCKET KEY OUTFILE
    s3.py list BUCKET [PREFIX]            (one key per line)
    s3.py delete BUCKET KEY
    s3.py delete-bucket BUCKET            (must be empty; a missing bucket is fine)

Exit 0 on success, 1 on an HTTP error (status and body tail on stderr), 2 on
usage / configuration errors.
"""
import datetime
import hashlib
import hmac
import http.client
import os
import re
import sys
import urllib.parse

REGION = "us-east-1"
SERVICE = "s3"
_UNRESERVED = "-_.~"


def _env(name):
    value = os.environ.get(name)
    if not value:
        print(f"s3.py: {name} is not set", file=sys.stderr)
        sys.exit(2)
    return value


def _sign(method, path, query, body, host, access_key, secret_key):
    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    payload_hash = hashlib.sha256(body).hexdigest()
    canonical_uri = urllib.parse.quote(path or "/", safe="/" + _UNRESERVED)
    canonical_qs = "&".join(
        f"{urllib.parse.quote(k, safe=_UNRESERVED)}={urllib.parse.quote(v, safe=_UNRESERVED)}"
        for k, v in sorted(query))
    headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
    signed = ";".join(sorted(headers))
    canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
    canonical = "\n".join([method, canonical_uri, canonical_qs, canonical_headers, signed,
                           payload_hash])
    scope = f"{date}/{REGION}/{SERVICE}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope,
                         hashlib.sha256(canonical.encode()).hexdigest()])

    def mac(key, msg):
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    key = mac(mac(mac(mac(("AWS4" + secret_key).encode(), date), REGION), SERVICE),
              "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                                f"SignedHeaders={signed}, Signature={signature}")
    return headers


def request(method, path, query=(), body=b""):
    endpoint = urllib.parse.urlsplit(_env("S3_ENDPOINT"))
    headers = _sign(method, path, list(query), body, endpoint.netloc,
                    _env("S3_ACCESS_KEY"), _env("S3_SECRET_KEY"))
    target = urllib.parse.quote(path or "/", safe="/" + _UNRESERVED)
    if query:
        target += "?" + "&".join(
            f"{urllib.parse.quote(k, safe=_UNRESERVED)}={urllib.parse.quote(v, safe=_UNRESERVED)}"
            for k, v in sorted(query))
    conn = http.client.HTTPConnection(endpoint.hostname, endpoint.port or 80, timeout=60)
    try:
        conn.request(method, target, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _fail(status, body, what):
    print(f"s3.py: {what} -> HTTP {status}: {body[-300:].decode(errors='replace')}",
          file=sys.stderr)
    sys.exit(1)


def main(argv):
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    op, args = argv[0], argv[1:]
    if op == "list-buckets" and not args:
        status, body = request("GET", "/")
        if status != 200:
            _fail(status, body, "list-buckets")
        for name in re.findall(rb"<Name>([^<]*)</Name>", body):
            print(name.decode())
    elif op == "create-bucket" and len(args) == 1:
        status, body = request("PUT", f"/{args[0]}")
        if status not in (200, 409):          # 409: already ours
            _fail(status, body, f"create-bucket {args[0]}")
    elif op == "head-bucket" and len(args) == 1:
        status, body = request("HEAD", f"/{args[0]}")
        if status != 200:
            _fail(status, body, f"head-bucket {args[0]}")
    elif op == "put" and len(args) == 3:
        with open(args[2], "rb") as f:
            data = f.read()
        status, body = request("PUT", f"/{args[0]}/{args[1]}", body=data)
        if status != 200:
            _fail(status, body, f"put {args[0]}/{args[1]}")
    elif op == "get" and len(args) == 3:
        status, body = request("GET", f"/{args[0]}/{args[1]}")
        if status != 200:
            _fail(status, body, f"get {args[0]}/{args[1]}")
        with open(args[2], "wb") as f:
            f.write(body)
    elif op == "list" and len(args) in (1, 2):
        keys, token = [], None
        while True:
            query = [("list-type", "2"), ("prefix", args[1] if len(args) == 2 else "")]
            if token:
                query.append(("continuation-token", token))
            status, body = request("GET", f"/{args[0]}", query)
            if status != 200:
                _fail(status, body, f"list {args[0]}")
            keys += [k.decode() for k in re.findall(rb"<Key>([^<]*)</Key>", body)]
            m = re.search(rb"<NextContinuationToken>([^<]*)</NextContinuationToken>", body)
            if b"<IsTruncated>true</IsTruncated>" in body and m:
                token = m.group(1).decode()
            else:
                break
        print("\n".join(keys))
    elif op == "delete" and len(args) == 2:
        status, body = request("DELETE", f"/{args[0]}/{args[1]}")
        if status not in (200, 204):
            _fail(status, body, f"delete {args[0]}/{args[1]}")
    elif op == "delete-bucket" and len(args) == 1:
        status, body = request("DELETE", f"/{args[0]}")
        if status not in (200, 204, 404):
            _fail(status, body, f"delete-bucket {args[0]}")
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
