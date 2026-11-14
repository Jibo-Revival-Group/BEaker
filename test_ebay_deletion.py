#!/usr/bin/env python3
"""eBay's account-deletion challenge: python3 test_ebay_deletion.py"""
from server import ebay_challenge_response

# printf '%s' 'abc123tok_en-1234567890123456789012345678https://joap.5x1.com/ebay/account-deletion' | sha256sum
assert ebay_challenge_response("abc123", "tok_en-1234567890123456789012345678") == \
    "53fa83edc2910fa263283e37d057711f9785c6b8ccb3012429f4417312dd74f5", "sha256 of code + token + endpoint URL"
print("PASS ebay deletion")
