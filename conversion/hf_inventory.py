#!/usr/bin/env python3
"""Record the public file inventory (path + size) of the CyberGym-E2E HF dataset
at a pinned revision. Only metadata is fetched (no gated content, no token needed).

    python3 conversion/hf_inventory.py <revision-sha> conversion/hf_inventory.json
"""
import json
import re
import sys
import urllib.request

REPO = "sunblaze-ucb/cybergym-e2e"


def main():
    rev, out_path = sys.argv[1], sys.argv[2]
    url = "https://huggingface.co/api/datasets/%s/tree/%s?recursive=true&expand=false" % (REPO, rev)
    entries = []
    while url:
        req = urllib.request.Request(url, headers={"User-Agent": "cge2e-inventory"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            entries += json.load(resp)
            m = re.search(r'<([^>]+)>;\s*rel="next"', resp.headers.get("Link") or "")
            url = m.group(1) if m else None
    files = {e["path"]: e["size"] for e in entries if e.get("type") == "file"}
    doc = {"repo": REPO, "revision": rev, "files": dict(sorted(files.items()))}
    with open(out_path, "w") as f:
        json.dump(doc, f, indent=0, sort_keys=False)
        f.write("\n")
    print("%d files recorded at %s" % (len(files), rev))


if __name__ == "__main__":
    main()
