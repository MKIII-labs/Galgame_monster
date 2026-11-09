"""看一眼 fixture 的嵌套结构，供写渲染自检页对字段用。"""
import io
import json

d = json.load(io.open("tests/fixtures/stock_ui.json", encoding="utf-8"))
print("top:", list(d.keys()))
st = d["state"]
print("state keys:", list(st.keys()))
g = st.get("game") or {}
print("game keys:", list(g.keys()))
print("cursor:", g.get("cursor_date"), "status:", g.get("status"))
print("players:", [(p.get("id"), p.get("kind"), len(p.get("positions") or [])) for p in (g.get("players") or [])])
print("symbols:", [(s.get("symbol"), s.get("name"), s.get("last")) for s in (g.get("symbols") or [])])
print("pending:", json.dumps(g.get("pending_orders"), ensure_ascii=False)[:400])
print("trades:", json.dumps((g.get("trades") or [])[:2], ensure_ascii=False)[:400])
bars = d["bars"]
print("bars type:", type(bars).__name__)
if isinstance(bars, dict):
    print("bars keys:", list(bars.keys()))
    k = list(bars.keys())[0]
    v = bars[k]
    print("bars[%s] type:" % k, type(v).__name__)
    if isinstance(v, dict):
        print("  keys:", list(v.keys()))
        arr = v.get("bars") or []
        print("  n bars:", len(arr), "first:", json.dumps(arr[:1], ensure_ascii=False)[:300])
        print("  marks:", json.dumps(v.get("marks"), ensure_ascii=False)[:300])
print("risk keys:", list((d.get("risk") or {}).keys()))
print("report keys:", list((d.get("report") or {}).keys()))
