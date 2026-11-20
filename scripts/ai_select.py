"""
주간교육뉴스 — 2단계: AI 선별·요약

하는 일
  1) 1단계 후보 목록(output/candidates-*.json)에서 점수 상위 N개 사건을 가져온다.
  2) 각 대표 기사의 본문을 읽어 온다. (data/bodies/ 에 저장해 두고 다시 읽지 않음)
  3) config/editorial.md(편집 기준)와 함께 Gemini(무료 등급)에 보내
     기사 5건 + 예비 5건을 고르고, 카드용 제목·강조문·요약·해시태그를 쓰게 한다.
  4) 결과를 점검한다: 본문에 없는 숫자, 길이 초과 등을 '확인 필요'로 표시.
  5) issues/<날짜>-vol<호수>/draft.yml 로 저장한다. 사람은 이 파일만 보고 고치면 된다.

실행
  GEMINI_API_KEY=... python scripts/ai_select.py          # Gemini 호출
  python scripts/ai_select.py --dry-run                     # 지시문(prompt.txt)만 만든다
  python scripts/ai_select.py --from-response 응답.json      # 다른 곳에서 받은 AI 응답을 넣는다
    → Gemini 한도가 막혔을 때 prompt.txt 를 Gemini·Claude 앱에 붙여 넣고,
      받은 JSON 을 저장해 이 옵션으로 넣으면 똑같이 진행된다.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import trafilatura
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from collect import bigrams, similar, tokens  # 같은 사건 판정 규칙을 1단계와 똑같이 쓴다

ROOT = Path(__file__).resolve().parent.parent
KST = timezone(timedelta(hours=9))
UA = "Mozilla/5.0 (compatible; CSERC-NewsBot/1.0; +http://www.cserc.or.kr)"
WEEKDAYS = "월화수목금토일"


# ---------------------------------------------------------------- 공통
class BlockStr(str):
    """여러 줄 문자열을 YAML에서 읽기 좋게 '|' 형식으로 쓰기 위한 표시"""


yaml.SafeDumper.add_representer(
    BlockStr, lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style="|"))


def load_settings():
    return yaml.safe_load((ROOT / "config/settings.yml").read_text(encoding="utf-8"))


def next_issue(settings, today=None):
    """다음 발행일 = 오늘 이후의 발행 요일 중, 마지막으로 발행한 호보다 뒤인 첫날"""
    today = today or datetime.now(KST).date()
    last_pub = None
    for f in (ROOT / "issues").glob("*/draft.yml"):
        y = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        if y.get("status") == "published":
            d = date.fromisoformat(str(y["date"]))
            last_pub = max(last_pub, d) if last_pub else d
    start = max(today, last_pub + timedelta(days=1)) if last_pub else today
    target = WEEKDAYS.index(settings["issue"]["publish_weekday"])
    pub = start + timedelta(days=(target - start.weekday()) % 7)
    vol = settings["issue"]["last_vol"] + 1
    return pub, vol, ROOT / "issues" / f"{pub.isoformat()}-vol{vol}"


def latest_candidates():
    files = sorted((ROOT / "output").glob("candidates-*.json"))
    if not files:
        sys.exit("후보 파일이 없습니다. 먼저 scripts/collect.py 를 실행하세요.")
    return json.loads(files[-1].read_text(encoding="utf-8"))


def past_titles(current_dir):
    """이미 발행(또는 확정)한 호의 기사 제목들 — 같은 사건을 다시 싣지 않기 위해"""
    out = []
    for f in sorted((ROOT / "issues").glob("*/draft.yml")):
        if f.parent == current_dir:
            continue
        y = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        if y.get("status") in ("confirmed", "published"):
            out += [(y["vol"], p["original_title"]) for p in y.get("picks", [])]
    return out


def drop_repeats(cands, past):
    def feat(t):
        return {"_bi": bigrams(t), "_tok": tokens(t)}
    past_f = [(vol, t, feat(t)) for vol, t in past]
    keep, dropped = [], []
    for c in cands:
        titles = [c["title"]] + [r["title"] for r in c["related"][:10]]
        hit = next(((vol, t) for vol, t, pf in past_f
                    if any(similar(feat(x), pf) for x in titles)), None)
        (dropped if hit else keep).append((c, hit))
    return [c for c, _ in keep], [(c, h) for c, h in dropped]


def find_selected(ids):
    """기사 고르기 화면에서 고른 후보 ID를 최근 수집 결과에서 찾는다 (고른 순서 유지)"""
    found = {}
    for f in sorted((ROOT / "output").glob("candidates-*.json"), reverse=True):
        for c in json.loads(f.read_text(encoding="utf-8"))["candidates"]:
            cid = c.get("id") or hashlib.md5(c["link"].encode()).hexdigest()[:6]
            if cid in ids and cid not in found:
                found[cid] = dict(c, id=cid)
    missing = [i for i in ids if i not in found]
    return [found[i] for i in ids if i in found], missing


# ---------------------------------------------------------------- 본문
def fetch_body(url, limit):
    cache = ROOT / "data/bodies" / (hashlib.md5(url.encode()).hexdigest() + ".txt")
    if cache.exists():
        return cache.read_text(encoding="utf-8")[:limit]
    text = ""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=25)
        text = trafilatura.extract(r.text, include_comments=False, include_tables=False,
                                   favor_precision=True) or ""
    except Exception:
        pass
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text, encoding="utf-8")
    return text[:limit]


# ---------------------------------------------------------------- 지시문
def build_prompt(editorial, cands, settings):
    n, alt = settings["issue"]["picks"], settings["issue"]["alternates"]
    blocks = []
    for c in cands:
        related = " / ".join(r["title"] for r in c["related"][:4])
        blocks.append(
            f"[후보 {c['rank']}] 점수 {c['score']} · {len(c['outlets'])}개 언론사 보도 · 분류: {', '.join(c['categories'][:3])}"
            f"{' · 의견기사' if c.get('opinion') else ''}\n"
            f"제목: {c['title']}\n언론사: {c['source']} ({c['published'][:10]})\n"
            f"같은 사건 다른 기사: {related or '없음'}\n"
            f"본문:\n{c['body'] or '(본문을 가져오지 못함 — 제목과 요약만 참고: ' + c['summary'] + ')'}\n")
    return f"""당신은 기독교학교교육연구소 '주간교육뉴스'의 편집자입니다.
아래 [편집 기준]을 따라 [후보 기사] 중에서 이번 호에 실을 기사 {n}건과 예비 기사 {alt}건을 고르고, 카드뉴스 문안을 쓰세요.

[편집 기준]
{editorial}

[출력 형식]
JSON 하나만 출력합니다.
{{
  "hashtags": ["해시태그6개"],
  "picks": [{{"cand": 후보번호, "category": "분류", "headline": "제목(\\n으로 줄바꿈)", "key_number": "숫자", "key_label": "숫자 설명",
             "points": ["핵심1", "핵심2", "핵심3"], "image_prompt": "영어 그림 설명", "highlight": "강조문", "summary": "요약", "why": "고른 이유 한 문장"}}],
  "alternates": [{{같은 형식}}]
}}
picks는 실을 순서대로 {n}개(가장 중요한 것이 1번), alternates는 {alt}개입니다.

[후보 기사]
{chr(10).join(blocks)}"""


TEXT_FIELDS = ("category", "headline", "key_number", "key_label", "image_prompt", "highlight", "summary", "why")
def build_prompt_selected(editorial, cands):
    """편집자가 고른 기사로만 문안을 쓰게 하는 지시문 (AI는 고르지 않는다)"""
    base = build_prompt(editorial, cands, {"issue": {"picks": len(cands), "alternates": 0}})
    return base.replace(
        f"[후보 기사] 중에서 이번 호에 실을 기사 {len(cands)}건과 예비 기사 0건을 고르고, 카드뉴스 문안을 쓰세요.",
        f"편집자가 이번 호에 실을 기사 {len(cands)}건을 이미 골랐습니다. 고르거나 빼지 말고, "
        f"[후보 기사]의 번호 순서 그대로 {len(cands)}건 모두 카드뉴스 문안을 쓰세요. alternates 는 빈 배열로 둡니다.")


SCHEMA_ITEM = {
    "type": "OBJECT",
    "properties": {k: {"type": "STRING"} for k in TEXT_FIELDS}
    | {"cand": {"type": "INTEGER"}, "points": {"type": "ARRAY", "items": {"type": "STRING"}}},
    "required": ["cand", "category", "headline", "points", "image_prompt", "highlight", "summary", "why"],
}
SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "hashtags": {"type": "ARRAY", "items": {"type": "STRING"}},
        "picks": {"type": "ARRAY", "items": SCHEMA_ITEM},
        "alternates": {"type": "ARRAY", "items": SCHEMA_ITEM},
    },
    "required": ["hashtags", "picks", "alternates"],
}


def call_gemini(prompt, settings, key):
    url = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "responseSchema": SCHEMA,
                             "temperature": 0.4},
    }
    last = None
    for model in (settings["ai"]["model"], settings["ai"]["fallback_model"]):
        for attempt in range(3):
            r = requests.post(url.format(model), json=body, timeout=180,
                              headers={"x-goog-api-key": key})
            if r.status_code == 200:
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text), model
            last = f"{model}: HTTP {r.status_code} {r.text[:200]}"
            if r.status_code in (429, 500, 503):
                time.sleep(20 * (attempt + 1))
                continue
            break
    raise RuntimeError(f"Gemini 호출 실패 — {last}")


# ---------------------------------------------------------------- 점검
NUM = re.compile(r"\d[\d,.]*")


def check_item(item, cand):
    """사람이 꼭 봐야 할 부분을 찾아 목록으로 돌려준다."""
    notes = []
    src = " ".join([cand["title"], cand.get("body", ""), cand["summary"]]
                   + [r["title"] for r in cand["related"]]).replace(",", "")
    texts = {f: item.get(f, "") for f in ("headline", "highlight", "summary", "key_number")}
    texts["points"] = " ".join(item.get("points") or [])
    for field, value in texts.items():
        for num in NUM.findall(value):
            n = num.rstrip(".,").replace(",", "")
            if n and n not in src:
                notes.append(f"{field}의 숫자 '{num}'가 본문에서 확인되지 않음")
    lines = item["headline"].split("\n")
    if len(lines) > 4 or any(len(l) > 17 for l in lines):
        notes.append("제목이 카드에 비해 김 (4줄, 줄당 16자 이내 권장)")
    if len(item["highlight"]) > 80:
        notes.append(f"강조문 {len(item['highlight'])}자 (80자 이내 권장)")
    pts = item.get("points") or []
    if len(pts) != 3 or any(len(x) > 38 for x in pts):
        notes.append("핵심 세 줄은 3줄, 줄당 34자 이내 권장")
    if not 170 <= len(item["summary"]) <= 300:
        notes.append(f"요약 {len(item['summary'])}자 (200~260자 권장)")
    if not cand.get("body"):
        notes.append("본문을 못 가져와 제목·요약만으로 작성됨")
    return list(dict.fromkeys(notes))  # 같은 지적은 한 번만


def to_entry(item, cand, no=None):
    entry = {}
    if no is not None:
        entry["order"] = no
    entry.update({
        "cand": item["cand"],
        "category": item["category"],
        "source": cand["source"],
        "link": cand["link"],
        "original_title": cand["title"],
        "headline": BlockStr(item["headline"].replace("\\n", "\n").strip()),
        "key_number": (item.get("key_number") or "").strip(),
        "key_label": (item.get("key_label") or "").strip(),
        "points": [x.strip() for x in (item.get("points") or [])][:3],
        "image_prompt": (item.get("image_prompt") or "").strip(),
        "highlight": item["highlight"].strip(),
        "summary": BlockStr(item["summary"].strip()),
        "why": item["why"].strip(),
    })
    notes = check_item({**item, "headline": entry["headline"]}, cand)
    if notes:
        entry["check"] = notes
    return entry


# ---------------------------------------------------------------- 실행
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="지시문만 만들고 끝냄")
    ap.add_argument("--from-response", help="다른 곳에서 받은 AI 응답 JSON 파일")
    ap.add_argument("--model-label", default=None, help="--from-response 로 넣을 때 기록할 작성자 이름")
    ap.add_argument("--force", action="store_true", help="이미 있는 초안을 새로 만들기 (사람이 고친 내용은 사라짐)")
    ap.add_argument("--ids", help="기사 고르기 화면에서 고른 후보 ID (쉼표, 고른 순서대로) — 이 기사로만 만든다")
    ap.add_argument("--request-issue", type=int, help="만들기 요청을 받은 GitHub 이슈 번호 (결과 댓글용)")
    args = ap.parse_args()

    settings = load_settings()
    editorial = (ROOT / "config/editorial.md").read_text(encoding="utf-8")
    data = latest_candidates()
    pub, vol, issue_dir = next_issue(settings)
    existing = issue_dir / "draft.yml"
    selected = None
    if args.ids:  # 사람이 고른 기사로 만들기 — 고른 것이 우선이므로 기존 초안(발행 전)은 새로 만든다
        ids = [i.strip() for i in re.split(r"[,\s]+", args.ids) if i.strip()]
        selected, missing = find_selected(ids)
        if missing:
            print(f"! 최근 수집 결과에서 찾지 못한 기사: {', '.join(missing)} (기사 고르기 화면을 새로고침한 뒤 다시 고르세요)")
        if not selected:
            sys.exit("고른 기사를 하나도 찾지 못했습니다.")
    elif existing.exists() and not args.force and not args.dry_run:
        old = yaml.safe_load(existing.read_text(encoding="utf-8")) or {}
        made = datetime.fromisoformat(str(old.get("generated_at", "2000-01-01T00:00+09:00")))
        # 이틀 안에 만든 초안은 사람이 고치는 중일 수 있으니 그대로 둔다.
        # 그보다 오래된 초안(예: 미리 해 본 시험)은 새 기사로 다시 만든다.
        if old.get("status") != "draft" or datetime.now(KST) - made < timedelta(days=2):
            print(f"이미 초안이 있습니다: {existing} — 새로 만들려면 --force")
            return
        print(f"{made:%m/%d}에 만든 오래된 초안이라 새 기사로 다시 만듭니다.")
    if selected:
        cands = selected
        for n, c in enumerate(cands, 1):
            c["rank"] = n  # 고른 순서가 카드 순서
    else:
        cands, dropped = drop_repeats(data["candidates"], past_titles(issue_dir))
        for c, (pvol, ptitle) in dropped:
            print(f"  지난 호(VOL.{pvol})와 같은 사건이라 제외: {c['title'][:50]}")
        cands = cands[: settings["ai"]["candidates_for_ai"]]
    for c in cands:
        c["body"] = fetch_body(c["link"], settings["ai"]["body_chars"])
    by_rank = {c["rank"]: c for c in cands}

    issue_dir.mkdir(parents=True, exist_ok=True)
    prompt = build_prompt_selected(editorial, cands) if selected else build_prompt(editorial, cands, settings)
    (issue_dir / "prompt.txt").write_text(prompt, encoding="utf-8")

    if args.from_response:
        resp, model = json.loads(Path(args.from_response).read_text(encoding="utf-8")), args.model_label or "수동 입력"
    elif args.dry_run or not os.environ.get("GEMINI_API_KEY"):
        print(f"지시문을 저장했습니다: {issue_dir / 'prompt.txt'} ({len(prompt):,}자)")
        if not args.dry_run:
            print("GEMINI_API_KEY 가 없어 AI 호출은 건너뛰었습니다.")
        return
    else:
        resp, model = call_gemini(prompt, settings, os.environ["GEMINI_API_KEY"])

    picks, alts = [], []
    if selected:
        got = {it["cand"]: it for it in resp["picks"] if it["cand"] in by_rank}
        for n, c in enumerate(cands, 1):
            it = got.get(n) or {
                "cand": n, "category": (c["categories"] or ["교육"])[0], "headline": c["title"], "key_number": "",
                "key_label": "", "points": [], "image_prompt": "A calm Korean school classroom scene",
                "highlight": "", "summary": "", "why": "편집자 선택 (AI 문안 없음 — 직접 채워 주세요)"}
            e = to_entry(it, c, n)
            e["why"] = "편집자 선택" + (f" — {it['why']}" if it.get("why") and n in got else " (AI 문안 없음 — 직접 채워 주세요)")
            picks.append(e)
        resp["picks"], resp["alternates"] = [], []
    for i, it in enumerate(resp["picks"], 1):
        if it["cand"] in by_rank:
            picks.append(to_entry(it, by_rank[it["cand"]], i))
    for it in resp["alternates"]:
        if it["cand"] in by_rank:
            alts.append(to_entry(it, by_rank[it["cand"]]))

    draft = {
        "vol": vol,
        "date": pub.isoformat(),
        "status": "draft",  # draft(초안) → confirmed(확정) → published(발행)
        "written_by": model,
        "generated_at": datetime.now(KST).isoformat(timespec="minutes"),
        "selected_by": "편집자" if selected else "AI",
        "request_issue": args.request_issue,
        "hashtags": [h.lstrip("#").replace(" ", "") for h in resp["hashtags"]][:6],
        "picks": picks,
        "alternates": alts,
        "events": [],  # 다가오는 연구소 행사 카드 (있을 때만) — 3단계에서 사용
    }
    out = issue_dir / "draft.yml"
    header = ("# 주간교육뉴스 초안 — 이 파일을 고치면 카드뉴스에 그대로 반영됩니다.\n"
              "# 기사 바꾸기: picks 의 항목을 지우고 alternates 의 항목을 옮겨 오세요.\n"
              "# 'check' 항목은 AI가 틀렸을 수 있는 부분입니다. 확인 후 지워도 됩니다.\n")
    out.write_text(header + yaml.safe_dump(draft, allow_unicode=True, sort_keys=False, width=1000),
                   encoding="utf-8")
    flagged = sum(1 for p in picks + alts if p.get("check"))
    print(f"VOL.{vol} ({pub}) 초안 저장: {out}")
    print(f"  선택 {len(picks)}건, 예비 {len(alts)}건, 확인 필요 {flagged}건 · 작성: {model}")
    for p in picks:
        print(f"  {p['order']}. [{p['category']}] {p['headline'].replace(chr(10), ' ')} — {p['source']}")


if __name__ == "__main__":
    main()
