"""Read the pack's JSON-quoted environment without evaluating shell code."""
import json
import os
import sys
from pathlib import Path

def load_environment(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        if line and not line.startswith("#"):
            key, raw = line.split("=", 1)
            value = json.loads(raw)
            if not isinstance(value, str) or any(ord(c) < 32 for c in value):
                raise ValueError("Invalid environment value")
            result[key] = value
    return result

if __name__ == "__main__":
    env = dict(os.environ, **load_environment(sys.argv[1]))
    os.execvpe(sys.argv[2], sys.argv[2:], env)
