"""
기독교학교교육연구소 주간교육뉴스 — 1단계: 기사 자동 수집기

하는 일
  1) config/sources.csv 의 언론사 RSS를 모두 읽는다.
  2) config/keywords.csv 의 키워드로 교육 관련 기사만 남긴다. (config/exclude.txt 의 단어는 제외)
  3) 매일 모은 기사를 data/pool/ 에 쌓는다. (큰 언론사 RSS는 몇 시간치만 보여 주므로 매일 돌려야 빠짐이 없다)
  4) 최근 N일치 기사를 '같은 사건'끼리 묶고, 중요도 점수를 매겨 후보 목록을 만든다.
     - 키워드 점수: 제목에 나오면 가중치 x2, 요약에만 나오면 x1
     - 보도량 점수: 같은 사건을 다룬 언론사가 많을수록 가산 (중요한 뉴스일수록 여러 곳이 다룬다)
     - 언론사 가중치: sources.csv 에서 조정
  5) output/ 에 후보 JSON 과 검토용 HTML 을 만든다.

실행:  python scripts/collect.py            (오늘 수집 + 최근 7일 후보 정리)
       python scripts/collect.py --days 1   (최근 1일만 후보 정리)
"""
import argparse
import csv
import hashlib
import html
import json
import math
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

ROOT = Path(__file__).resolve().parent.parent
KST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (compatible; CSERC-NewsBot/1.0; +http://www.cserc.or.kr)"


# ---------------------------------------------------------------- 설정 읽기
def load_sources():
    rows = []
    with open(ROOT / "config/sources.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("사용", "Y").strip().upper() != "Y":
                continue
            rows.append({
                "name": r["이름"].strip(),
                "type": r["분류"].strip(),
                "url": r["주소"].strip(),
                "weight": float(r.get("가중치") or 1.0),
            })
    return rows


def load_keywords():
    rows = []
    with open(ROOT / "config/keywords.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("사용", "Y").strip().upper() != "Y":
                continue
            rows.append({
                "kw": r["키워드"].strip(),
                "cat": r["분류"].strip(),
                "w": float(r.get("가중치") or 1),
            })
    # 긴 키워드부터 검사해야 '학교 밖 청소년'이 '청소년'보다 먼저 잡힌다
    rows.sort(key=lambda x: -len(x["kw"]))
    return rows


def load_excludes():
    words = []
    for line in (ROOT / "config/exclude.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            words.append(line)
    return words


# ---------------------------------------------------------------- 수집
def clean_text(s):
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def entry_time(e):
    for key in ("published_parsed", "updated_parsed"):
        t = e.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc).astimezone(KST)
    return None


def fetch_source(src):
    feed, err = None, None
    for attempt in range(2):  # 일시적 오류에 대비해 한 번 더 시도
        try:
            r = requests.get(src["url"], headers={"User-Agent": UA}, timeout=40)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
            break
        except Exception as ex:  # 한 언론사가 실패해도 전체는 계속 진행
            err = ex
            time.sleep(3)
    if feed is None:
        return src, [], f"실패: {type(err).__name__}"
    items = []
    for e in feed.entries:
        title = clean_text(e.get("title"))
        link = (e.get("link") or "").strip()
        if not title or not link:
            continue
        items.append({
            "title": title,
            "link": link,
            "summary": clean_text(e.get("summary") or e.get("description"))[:400],
            "published": (entry_time(e) or datetime.now(KST)).isoformat(),
            "source": src["name"],
            "source_type": src["type"],
            "source_weight": src["weight"],
        })
    return src, items, f"{len(items)}건"


# 제목에 이런 표시가 있으면 '의견 기사'로 보고 점수를 절반으로 (보도 기사 우선)
OPINION_MARKS = ["[기고]", "[칼럼]", "칼럼]", "[시론]", "[사설]", "[기자수첩]", "[오피니언]",
                 "[논단]", "[시평]", "[발언대]", "[특별기고]", "[독자투고]", "[교단일기]", "[시선]"]
# 연구소 고유 관심 분야 — 검토 화면 맨 위에 따로 모아 보여 준다
MISSION_CATS = ["기독교교육", "대안교육", "사학·종교"]


def match_keywords(item, keywords):
    title, summary = item["title"], item["summary"]
    hits, used_spans = [], []
    score = 0.0
    for k in keywords:
        kw = k["kw"]
        in_title = kw in title
        in_sum = kw in summary
        if not (in_title or in_sum):
            continue
        # 이미 더 긴 키워드로 잡힌 부분이면 건너뜀 ('학교 밖 청소년' 안의 '청소년')
        if any(kw in longer for longer in used_spans):
            continue
        used_spans.append(kw)
        score += k["w"] * (2 if in_title else 1)
        hits.append({"kw": kw, "cat": k["cat"], "where": "제목" if in_title else "요약"})
    item["opinion"] = any(m in title for m in OPINION_MARKS)
    if item["opinion"]:
        score *= 0.5
    return score, hits


# ---------------------------------------------------------------- 같은 사건 묶기
STOP = set("단독 속보 종합 영상 포토 기자 뉴스 오늘 내일 올해 내년 지난 관련 위해 대한 통해 이번 있다 없다".split())


def norm_title(t):
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)|<[^>]*>", " ", t)
    t = re.sub(r"[\"'“”‘’·…,.!?:;~\-–—|/]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def bigrams(t):
    t = norm_title(t).replace(" ", "")
    return {t[i:i + 2] for i in range(len(t) - 1)}


def tokens(t):
    out = set()
    for w in norm_title(t).split():
        w = re.sub(r"(은|는|이|가|을|를|의|에|에서|으로|로|와|과|도|만|까지|부터)$", "", w)
        if len(w) >= 2 and w not in STOP:
            out.add(w)
    return out


def similar(a, b):
    ba, bb = a["_bi"], b["_bi"]
    jac = len(ba & bb) / max(1, len(ba | bb))
    shared = len(a["_tok"] & b["_tok"])
    return jac >= 0.33 or (shared >= 3) or (shared >= 2 and jac >= 0.22)


def cluster(items):
    for it in items:
        it["_bi"], it["_tok"] = bigrams(it["title"]), tokens(it["title"])
    clusters = []
    for it in sorted(items, key=lambda x: -x["kw_score"]):
        for c in clusters:
            if any(similar(it, m) for m in c["items"][:6]):
                c["items"].append(it)
                break
        else:
            clusters.append({"items": [it]})
    for c in clusters:
        for it in c["items"]:
            it.pop("_bi", None), it.pop("_tok", None)
    return clusters


def score_cluster(c):
    items = c["items"]
    lead = max(items, key=lambda x: (x["kw_score"] * x["source_weight"], -len(x["title"])))
    outlets = sorted({i["source"] for i in items})
    cats = {}
    for i in items:
        for h in i["hits"]:
            cats[h["cat"]] = cats.get(h["cat"], 0) + 1
    kw_part = lead["kw_score"] * lead["source_weight"]
    coverage = 4 * math.log2(len(outlets)) if len(outlets) > 1 else 0  # 2곳 +4, 4곳 +8, 8곳 +12
    c.update({
        "lead": lead,
        "outlets": outlets,
        "categories": sorted(cats, key=lambda k: -cats[k]),
        "score": round(kw_part + coverage, 1),
        "first_seen": min(i["published"] for i in items),
    })
    return c


# ---------------------------------------------------------------- 저장·출력
def pool_path(day):
    return ROOT / "data/pool" / f"{day}.json"


def load_pool(days):
    today = datetime.now(KST).date()
    seen, out = set(), []
    for d in range(days):
        p = pool_path((today - timedelta(days=d)).isoformat())
        if p.exists():
            for it in json.loads(p.read_text(encoding="utf-8")):
                if it["link"] not in seen:
                    seen.add(it["link"])
                    out.append(it)
    return out


def write_html(clusters, meta, path, mission=()):
    cat_colors = {
        "기독교교육": "#b4442b", "대안교육": "#b4442b", "사학·종교": "#9a5b13",
        "교육정책": "#1f5f8b", "입시·평가": "#41607a", "교사·교권": "#3c6e47",
        "학생·학부모": "#6a4c93", "학교현장": "#55606b", "인구·학교구조": "#7a5a2f",
        "디지털·AI": "#2a7a7a",
    }
    def render(c, rank):
        lead = c["lead"]
        tags = "".join(
            f'<span class="tag" style="background:{cat_colors.get(cat, "#666")}">{html.escape(cat)}</span>'
            for cat in c["categories"][:3])
        others = "".join(
            f'<li><a href="{html.escape(i["link"])}" target="_blank">{html.escape(i["title"])}</a> <em>{html.escape(i["source"])}</em></li>'
            for i in c["items"][1:8] if i is not lead)
        more = f'<details><summary>같은 사건 다른 기사 {len(c["items"]) - 1}건</summary><ul>{others}</ul></details>' if len(c["items"]) > 1 else ""
        kws = ", ".join(sorted({h["kw"] for i in c["items"] for h in i["hits"]}))
        op = '<span class="tag" style="background:#999">의견</span>' if lead.get("opinion") else ""
        return (f"""
<article>
  <div class="rank">{rank}</div>
  <div class="body">
    <div class="meta">{tags}{op}<span class="score">점수 {c['score']}</span><span>{len(c['outlets'])}개 언론사</span><span>{lead['published'][:16].replace('T', ' ')}</span></div>
    <h3><a href="{html.escape(lead['link'])}" target="_blank">{html.escape(lead['title'])}</a></h3>
    <p class="src">{html.escape(lead['source'])} · 보도: {html.escape(', '.join(c['outlets']))}</p>
    <p class="sum">{html.escape(lead['summary'][:180])}</p>
    <p class="kw">키워드: {html.escape(kws)}</p>
    {more}
  </div>
</article>""")

    rows = [render(c, n) for n, c in enumerate(clusters, 1)]
    mission_rows = [render(c, "★") for c in mission]
    src_rows = "".join(
        f"<li>{html.escape(n)} — {html.escape(s)}</li>" for n, s in meta["source_status"])
    doc = f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>주간교육뉴스 후보 기사 — {meta['generated'][:10]}</title>
<style>
body{{font-family:'Pretendard','Apple SD Gothic Neo','Malgun Gothic',sans-serif;margin:0;background:#f4f1ea;color:#222}}
header{{background:#1d3b2a;color:#fff;padding:22px 20px}}
header h1{{margin:0 0 6px;font-size:22px}} header p{{margin:0;opacity:.85;font-size:14px}}
main{{max-width:860px;margin:0 auto;padding:16px}}
article{{display:flex;gap:14px;background:#fff;border-radius:10px;padding:14px 16px;margin:10px 0;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
.rank{{font-size:22px;font-weight:800;color:#e36b3c;min-width:34px;text-align:center}}
.body{{flex:1;min-width:0}} h3{{margin:6px 0 4px;font-size:17px;line-height:1.4}} h3 a{{color:#111;text-decoration:none}}
.meta{{display:flex;flex-wrap:wrap;gap:6px;font-size:12px;color:#666;align-items:center}}
.tag{{color:#fff;border-radius:4px;padding:2px 7px;font-weight:600}} .score{{font-weight:700;color:#e36b3c}}
.src{{font-size:13px;color:#555;margin:2px 0}} .sum{{font-size:14px;color:#333;margin:6px 0;line-height:1.5}}
.kw{{font-size:12px;color:#888;margin:4px 0}} details{{font-size:13px;color:#444}} details li{{margin:3px 0}} em{{color:#888;font-style:normal}}
h2{{font-size:16px;margin:22px 4px 4px;color:#1d3b2a}}
footer{{max-width:860px;margin:0 auto 40px;padding:0 16px;font-size:12px;color:#666}}
</style></head><body>
<header><h1>주간교육뉴스 후보 기사</h1>
<p>{meta['generated'][:16].replace('T', ' ')} 기준 · 최근 {meta['days']}일 · 수집 {meta['collected']}건 → 교육 관련 {meta['matched']}건 → 사건 {meta['clusters']}개로 묶음 · 상위 {len(clusters)}개 표시</p></header>
<main>
<h2>연구소 관심 분야 (기독교교육·대안교육·사학·종교)</h2>{''.join(mission_rows) or '<p>이번 기간에는 없음</p>'}
<h2>전체 중요도 순위</h2>{''.join(rows)}</main>
<footer><details><summary>언론사별 수집 결과</summary><ul>{src_rows}</ul></details></footer>
</body></html>"""
    path.write_text(doc, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="후보 정리에 쓸 최근 일수")
    ap.add_argument("--top", type=int, default=40, help="후보 몇 개까지 보여줄지")
    ap.add_argument("--min-score", type=float, default=5.0, help="이 점수 미만은 버림")
    args = ap.parse_args()

    now = datetime.now(KST)
    today = now.date().isoformat()
    sources, keywords, excludes = load_sources(), load_keywords(), load_excludes()

    # 1) 수집
    collected, statuses = [], []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(fetch_source, s) for s in sources]
        for f in as_completed(futs):
            src, items, status = f.result()
            statuses.append((src["name"], status))
            collected.extend(items)
    statuses.sort()

    # 2) 필터: 기간, 제외어, 키워드
    cutoff = now - timedelta(days=args.days)
    matched = []
    for it in collected:
        if datetime.fromisoformat(it["published"]) < cutoff:
            continue
        if any(w in it["title"] for w in excludes):
            continue
        score, hits = match_keywords(it, keywords)
        if not hits:
            continue
        it["kw_score"], it["hits"] = score, hits
        matched.append(it)

    # 3) 오늘 풀에 저장(누적) — 이미 저장된 링크는 건너뜀
    pool_file = pool_path(today)
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(pool_file.read_text(encoding="utf-8")) if pool_file.exists() else []
    known = {i["link"] for i in existing}
    existing.extend(i for i in matched if i["link"] not in known)
    pool_file.write_text(json.dumps(existing, ensure_ascii=False, indent=1), encoding="utf-8")

    # 3-1) 3주가 지난 수집 자료는 지운다 (저장 공간 관리)
    old_day = (now - timedelta(days=21)).date().isoformat()
    for f in (ROOT / "data/pool").glob("*.json"):
        if f.stem < old_day:
            f.unlink()
    for f in (ROOT / "data/bodies").glob("*.txt") if (ROOT / "data/bodies").exists() else []:
        if f.stat().st_mtime < time.time() - 21 * 86400:
            f.unlink()

    for f in (ROOT / "output").glob("*-20*.*") if (ROOT / "output").exists() else []:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", f.name)
        if m and m.group(1) < old_day:
            f.unlink()

    # 4) 최근 N일 풀 → 묶기 → 점수
    #    키워드·제외어를 바꾸면 이미 모은 기사에도 바로 반영되도록 매번 다시 채점한다
    pool = []
    for it in load_pool(args.days):
        if datetime.fromisoformat(it["published"]) < cutoff:
            continue
        if any(w in it["title"] for w in excludes):
            continue
        score, hits = match_keywords(it, keywords)
        if hits:
            it["kw_score"], it["hits"] = score, hits
            pool.append(it)
    clusters = [score_cluster(c) for c in cluster(pool)]
    clusters = [c for c in clusters if c["score"] >= args.min_score]
    clusters.sort(key=lambda c: -c["score"])
    top = clusters[: args.top]

    # 5) 출력
    out_dir = ROOT / "output"
    out_dir.mkdir(exist_ok=True)
    meta = {
        "generated": now.isoformat(), "days": args.days, "collected": len(collected),
        "matched": len(pool), "clusters": len(clusters), "source_status": statuses,
    }
    (out_dir / f"candidates-{today}.json").write_text(json.dumps({
        "meta": meta,
        "candidates": [{
            "id": hashlib.md5(c["lead"]["link"].encode()).hexdigest()[:6],
            "rank": n, "score": c["score"], "categories": c["categories"], "outlets": c["outlets"],
            "keywords": sorted({h["kw"] for i in c["items"] for h in i["hits"]}),
            "title": c["lead"]["title"], "link": c["lead"]["link"], "source": c["lead"]["source"],
            "summary": c["lead"]["summary"], "published": c["lead"]["published"],
            "opinion": c["lead"].get("opinion", False),
            "related": [{"title": i["title"], "link": i["link"], "source": i["source"]}
                        for i in c["items"] if i is not c["lead"]],
        } for n, c in enumerate(top, 1)],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    mission = [c for c in clusters
               if any(cat in MISSION_CATS for cat in c["categories"]) and c["score"] >= 10][:8]
    write_html(top, meta, out_dir / f"review-{today}.html", mission)

    print(f"수집 {len(collected)}건 / 교육 관련 {len(pool)}건 / 사건 {len(clusters)}개 → 상위 {len(top)}개")
    for n, s in statuses:
        if not s.endswith("건") or s.startswith("0"):
            print(f"  ! {n}: {s}")
    for n, c in enumerate(top[:15], 1):
        print(f"{n:2d}. [{c['score']:5.1f}] ({len(c['outlets'])}곳) {c['lead']['title'][:60]} — {c['lead']['source']} | {','.join(c['categories'][:2])}")


if __name__ == "__main__":
    sys.exit(main())
