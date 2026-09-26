#!/usr/bin/env python3
"""Bundle index.html + Chart.js + data/*.json into ONE self-contained
em-macro-dashboard.html that opens by double-click (file://), no server needed.

    python fetch_em.py && python build_single.py
    python build_single.py --data /tmp/demo --out demo.html
"""
import argparse, json, re
from pathlib import Path

ROOT = Path(__file__).parent
ap = argparse.ArgumentParser()
ap.add_argument("--data", default=str(ROOT / "data"))
ap.add_argument("--out", default=str(ROOT / "em-macro-dashboard.html"))
a = ap.parse_args()

data = {p.stem: json.loads(p.read_text()) for p in sorted(Path(a.data).glob("*.json"))}
assert "_meta" in data, f"no _meta.json in {a.data} - run fetch_em.py first"
html = (ROOT / "index.html").read_text()
chartjs = (ROOT / "vendor" / "chart.umd.js").read_text().replace("</script>", "<\\/script>")
payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
html, n = re.subn(r'<!--CHARTJS--><script src="[^"]+"></script>',
                  lambda m: f"<script>{chartjs}</script>\n<script>window.__EM_DATA__={payload};</script>", html)
assert n == 1, "Chart.js placeholder not found in index.html"
Path(a.out).write_text(html)
print(f"wrote {a.out} ({len(html)/1024:.0f} KB)")
