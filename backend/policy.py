"""
policy.py
---------
Thin loader for UMN academic policy rules.

All policy values (credit minimums, WI rules, Liberal Ed mappings, course
filters, meta-requirement keywords) live in umn_policy.json so they can be
audited and updated without touching algorithm code. Import POLICY from here.
"""

import json
import os

_path = os.path.join(os.path.dirname(__file__), "umn_policy.json")
with open(_path) as f:
    POLICY = json.load(f)
