#!/usr/bin/env python3
"""
set_fee_recipient.py — Set Lighthouse per-validator fee recipient via Keymanager API.

Constraints:
- Pubkey must be provided (0x + 96 hex chars).
- No retries; a timeout (>10s) is a permanent error.
- No environment variables are used.
- --token-file defaults to ./api-token.txt
- --vc-url defaults to http://localhost:5062

Example:
  ./set_fee_recipient.py \
      --pubkey 0xabcdef... (96 hex chars) \
      --fee-recipient 0x25c4a76E7d118705e7Ea2e9b7d8C59930d8aCD3b \
      --vc-url http://localhost:5062 \
      --token-file ./api-token.txt
"""

import argparse
import os
import sys
from typing import Tuple

import requests

TIMEOUT_SECS = 10.0  # fixed; do not retry


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Set Lighthouse per-validator fee recipient")
    p.add_argument("--pubkey", "-p", required=True,
                   help="BLS pubkey (0x + 96 hex chars)")
    p.add_argument("--fee-recipient", "-r", required=True,
                   help="0x-prefixed Ethereum address (40 hex chars)")
    p.add_argument("--vc-url", default="http://localhost:5062",
                   help="Validator Client base URL (default: %(default)s)")
    p.add_argument("--token-file", "-t", default="api-token.txt",
                   help="Path to VC API token file (default: ./api-token.txt)")
    return p.parse_args()


def validate_pubkey(pubkey: str) -> str:
    pk = pubkey.strip()
    if not pk.startswith("0x"):
        pk = "0x" + pk
    if not (pk.startswith("0x") and len(pk) == 98):
        die("pubkey must be 0x + 96 hex chars (48 bytes)")
    try:
        bytes.fromhex(pk[2:])
    except ValueError:
        die("pubkey is not valid hex")
    return pk


def validate_eth_address(addr: str) -> str:
    a = addr.strip()
    if not (a.startswith("0x") and len(a) == 42):
        die("fee recipient must be 0x + 40 hex chars")
    try:
        bytes.fromhex(a[2:])
    except ValueError:
        die("fee recipient is not valid hex")
    return a


def read_token(path: str) -> str:
    if not os.path.isfile(path):
        die(f"token file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            token = f.read().strip()
    except OSError as e:
        die(f"error reading token file {path}: {e}")
    if not token:
        die(f"token file {path} is empty")
    return token


def post_fee_recipient(vc_url: str, token: str, pubkey: str, addr: str) -> Tuple[int, str]:
    url = f"{vc_url.rstrip('/')}/eth/v1/validator/{pubkey}/feerecipient"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {"ethaddress": addr}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT_SECS)
        return r.status_code, (r.text or "")
    except requests.Timeout:
        die(f"request timed out after {TIMEOUT_SECS:.0f}s (treating as permanent error)")
    except requests.RequestException as e:
        die(f"request error: {e}")
    return 0, ""  # unreachable


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    args = parse_args()
    pubkey = validate_pubkey(args.pubkey)
    addr = validate_eth_address(args.fee_recipient)
    token = read_token(args.token_file)

    status, body = post_fee_recipient(args.vc_url, token, pubkey, addr)
    if status == 202:
        print(f"OK: fee recipient set to {addr} for {pubkey}")
        return
    if status == 401:
        die("unauthorized: check token and VC HTTP settings/URL")
    if status == 404:
        die("validator not found (VC has not loaded this pubkey?)")
    die(f"unexpected status {status}: {body.strip() or '<no body>'}")


if __name__ == "__main__":
    main()
