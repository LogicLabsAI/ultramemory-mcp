#!/usr/bin/env python3
import json, sys
mf, dest, field = sys.argv[1], sys.argv[2], sys.argv[3]
data = json.load(open(mf))
for f in data.get("files", []):
    if f.get("path") == "agent-kit/" + dest or f.get("path") == dest:
        print(f.get(field, "")); break
