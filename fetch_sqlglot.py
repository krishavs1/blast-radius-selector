"""Download sqlglot at the pinned commit into /opt/sqlglot (build time only)."""
import io
import os
import sys
import tarfile
import urllib.request

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URL = "https://github.com/tobymao/sqlglot/archive/%s.tar.gz" % COMMIT
DEST = "/opt/sqlglot"

data = urllib.request.urlopen(URL, timeout=120).read()
tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
prefix = "sqlglot-%s/" % COMMIT
os.makedirs(DEST, exist_ok=True)
for m in tar.getmembers():
    if not m.name.startswith(prefix):
        continue
    m.name = m.name[len(prefix):]
    if not m.name:
        continue
    tar.extract(m, DEST, filter="data")
print("fetched", len(data), "bytes", file=sys.stderr)
