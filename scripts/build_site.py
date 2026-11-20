"""
주간교육뉴스 — 3·4단계: 뉴스 페이지와 발송 문안 만들기

하는 일
  1) 발행된 호(status: published)를 모아 site/ 에 뉴스 페이지를 만든다. (GitHub Pages 로 무료 공개)
       site/index.html          전체 호 목록
       site/vol227/index.html   호별 페이지 (카드 슬라이드 + 기사 5건 글 + 원문 링크)
       site/feed.xml            RSS
  2) 각 호의 발송 문안을 issues/<호>/out/ 에 만든다.
       kakao.txt     단체방·오픈채팅방·채널에 붙여 넣을 글 (링크를 넣으면 표지 미리보기가 뜸)
       email.html    메일 본문,  email_subject.txt  메일 제목
       board.txt     기존 홈페이지 게시판에 올릴 제목·본문

실행:  python scripts/build_site.py              (발행된 호만)
       python scripts/build_site.py --preview    (초안도 /preview/ 아래에 미리보기로 포함)
"""
import argparse
import shutil
from datetime import date, datetime, timezone
from email.utils import format_datetime
from html import escape
from pathlib import Path

import sys

import yaml
from jinja2 import Environment, FileSystemLoader
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import images  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ORDINAL = {1: "첫째", 2: "둘째", 3: "셋째", 4: "넷째", 5: "다섯째"}
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"


def week_label(d: date):
    return f"{d.month}월 {ORDINAL[(d.day - 1) // 7 + 1]}주"


def load_issues(include_drafts):
    issues = []
    for f in sorted((ROOT / "issues").glob("*/draft.yml")):
        y = yaml.safe_load(f.read_text(encoding="utf-8"))
        cards = sorted((f.parent / "cards").glob("*.jpg"))
        if not cards:
            continue  # 카드가 없는 호(예: 수동 제작한 과거 호)는 건너뜀
        if y["status"] == "published" or (include_drafts and y["status"] in ("draft", "confirmed")):
            y["_dir"], y["_cards"] = f.parent, cards
            issues.append(y)
    return issues


def prepare(y, site, preview):
    d = date.fromisoformat(str(y["date"]))
    slug = f"vol{y['vol']}"
    rel = f"preview/{slug}" if (preview and y["status"] != "published") else slug
    folder = f"{site['base_url']}/{rel}/"
    # 로컬 파일로 볼 때는 폴더 주소가 열리지 않으므로 index.html 을 붙인다
    url = folder + ("index.html" if site["base_url"].startswith("file:") else "")
    picks = []
    for n, p in enumerate(y["picks"], 1):
        q = dict(p)
        # 기사 사진: 카드와 같은 규칙 (images.choose)
        src, kind, credit = images.choose(y["_dir"], p)
        if isinstance(src, str):
            q["photo"] = src
        elif src is not None and Path(src).exists():
            q["photo_file"], q["photo"] = Path(src), f"{folder}photo-{n}.jpg"
        q["photo_credit"] = credit if q.get("photo") else ""
        q["headline_line"] = " ".join(l.strip() for l in str(p["headline"]).split("\n") if l.strip())
        q["summary"] = str(p["summary"]).strip()
        picks.append(q)
    return {
        "vol": y["vol"], "date": d.isoformat(), "date_label": week_label(d),
        "title": f"{d.year}년 {week_label(d)} 주간교육뉴스",
        "hashtags": y.get("hashtags", []), "picks": picks, "status": y["status"],
        "rel": rel, "url": url, "dir": y["_dir"], "card_files": y["_cards"],
        "cards": [f"{folder}{c.name}" for c in y["_cards"]],
        "cover_url": f"{folder}{y['_cards'][0].name}", "thumb": f"{folder}thumb.jpg",
    }


def write_messages(i, site):
    out = i["dir"] / "out"
    out.mkdir(exist_ok=True)
    lines = [f"📚 주간교육뉴스 VOL.{i['vol']} | {i['date_label']}",
             f"{site['org']}가 고른 이번 주 교육 소식", ""]
    lines += [f"{CIRCLED[n]} {p['headline_line']}" for n, p in enumerate(i["picks"])]
    lines += ["", f"👉 카드뉴스 보기: {i['url']}"]
    (out / "kakao.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    first = i["picks"][0]["headline_line"]
    (out / "email_subject.txt").write_text(
        f"[주간교육뉴스 VOL.{i['vol']}] {first} 외 {len(i['picks']) - 1}건\n", encoding="utf-8")

    board = [f"제목: {i['title']}", "",
             f"첨부할 이미지: cards 폴더의 {len(i['card_files'])}장 ({', '.join(c.name for c in i['card_files'])})",
             "", "본문:", f"주간교육뉴스 VOL.{i['vol']} — 기사 원문과 요약은 아래 링크에서 보실 수 있습니다.", i["url"]]
    (out / "board.txt").write_text("\n".join(board) + "\n", encoding="utf-8")


def build(include_drafts=False, base_url=None):
    settings = yaml.safe_load((ROOT / "config/settings.yml").read_text(encoding="utf-8"))
    site = settings["site"]
    if base_url:  # 화면 확인용으로 주소를 바꿔 만들 때
        site["base_url"] = base_url.rstrip("/")
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=True)
    dist = ROOT / "site"
    if dist.exists():
        shutil.rmtree(dist)
    (dist / "assets").mkdir(parents=True)
    shutil.copy(ROOT / "assets/img/emblem.png", dist / "assets/emblem.png")

    raw = load_issues(include_drafts)
    issues = [prepare(y, site, include_drafts) for y in raw]
    issues.sort(key=lambda i: i["vol"], reverse=True)
    published = [i for i in issues if i["status"] == "published"]

    for n, i in enumerate(issues):
        target = dist / i["rel"]
        target.mkdir(parents=True, exist_ok=True)
        for c in i["card_files"]:
            shutil.copy(c, target / c.name)
        for n, p in enumerate(i["picks"], 1):
            if p.get("photo_file"):
                shutil.copy(p["photo_file"], target / f"photo-{n}.jpg")
        Image.open(i["card_files"][0]).convert("RGB").resize((540, 540), Image.LANCZOS).save(
            target / "thumb.jpg", quality=85)
        pos = published.index(i) if i in published else None
        newer = published[pos - 1] if pos not in (None, 0) else None
        older = published[pos + 1] if pos is not None and pos + 1 < len(published) else None
        page = env.get_template("site_issue.html").render(
            site=site, issue=i, prev=older, next=newer,
            page_title=f"{i['title']} (VOL.{i['vol']}) | {site['org']}",
            description=" ".join("#" + t for t in i["hashtags"]),
            canonical=i["url"], og_image=i["cover_url"])
        if i["status"] != "published":
            page = page.replace("<head>", '<head>\n<meta name="robots" content="noindex">', 1)
        (target / "index.html").write_text(page, encoding="utf-8")
        (target / "email.html").write_text(
            env.get_template("email.html").render(site=site, issue=i), encoding="utf-8")
        write_messages(i, site)
        shutil.copy(target / "email.html", i["dir"] / "out" / "email.html")

    latest = published[0] if published else (issues[0] if issues else None)
    (dist / "index.html").write_text(env.get_template("site_index.html").render(
        site=site, latest=latest, issues=published or issues[:1],
        weekday=settings["issue"]["publish_weekday"],
        page_title=f"{site['org']} {site['title']}",
        description="학부모와 교사가 꼭 알아야 할 한 주의 교육 소식",
        canonical=site["base_url"] + "/", og_image=latest["cover_url"] if latest else ""), encoding="utf-8")

    items = "".join(
        f"<item><title>{escape(i['title'])} (VOL.{i['vol']})</title><link>{i['url']}</link><guid>{i['url']}</guid>"
        f"<pubDate>{format_datetime(datetime.fromisoformat(i['date'] + 'T10:00:00+09:00'))}</pubDate>"
        f"<description>{escape(' / '.join(p['headline_line'] for p in i['picks']))}</description></item>"
        for i in published[:30])
    (dist / "feed.xml").write_text(
        f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>{escape(site["org"])} 주간교육뉴스</title>'
        f'<link>{site["base_url"]}/</link><description>한 주의 교육 소식</description>{items}</channel></rss>',
        encoding="utf-8")
    (dist / ".nojekyll").write_text("")
    write_kakao_auth_page(dist)
    write_review_page(dist, settings, env)
    return issues


CAT_COLORS = {
    "기독교교육": "#b4442b", "대안교육": "#b4442b", "사학·종교": "#9a5b13", "교육정책": "#1f5f8b",
    "입시·평가": "#41607a", "교사·교권": "#3c6e47", "학생·학부모": "#6a4c93", "학교현장": "#55606b",
    "인구·학교구조": "#7a5a2f", "디지털·AI": "#2a7a7a",
}
MISSION_CATS = ("기독교교육", "대안교육", "사학·종교")


def write_review_page(dist, settings, env):
    """기사 고르기 화면 (site/review/) — 가장 최근 수집 결과로 만든다."""
    import json
    import os
    import hashlib
    from ai_select import next_issue
    files = sorted((ROOT / "output").glob("candidates-*.json"))
    if not files:
        return
    data = json.loads(files[-1].read_text(encoding="utf-8"))
    cands = data["candidates"]
    for c in cands:
        c.setdefault("id", hashlib.md5(c["link"].encode()).hexdigest()[:6])
        c.setdefault("keywords", [])
    mission = [c for c in cands if any(cat in MISSION_CATS for cat in c["categories"]) and c["score"] >= 10][:8]
    cats = [k for k in CAT_COLORS if any(k in c["categories"] for c in cands)]
    pub, vol, _ = next_issue(settings)
    repo = os.environ.get("GITHUB_REPOSITORY") or settings["site"].get("repo", "")
    page = env.get_template("review.html").render(
        meta=data["meta"], cands=cands, mission=mission, categories=cats, colors=CAT_COLORS,
        next_vol=vol, next_date=pub.isoformat(), repo=repo,
        info={c["id"]: {"title": c["title"], "source": c["source"]} for c in cands})
    (dist / "review").mkdir(exist_ok=True)
    (dist / "review" / "index.html").write_text(page, encoding="utf-8")


def write_kakao_auth_page(dist):
    """카카오 로그인 후 돌아오는 페이지 — 주소창의 code 값을 보여 주고 복사하게 한다."""
    import os
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    actions = f"https://github.com/{repo}/actions/workflows/kakao-connect.yml" if repo else "#"
    (dist / "kakao-auth.html").write_text(f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>카카오 연결 코드</title></head>
<body style="font-family:sans-serif;max-width:560px;margin:40px auto;padding:0 18px;line-height:1.6">
<h2>카카오 연결 코드</h2>
<p>아래 코드를 복사해 GitHub의 <b>'4. 카카오 연결'</b> 작업에 붙여 넣고 실행하세요. 코드는 몇 분 안에 만료됩니다.</p>
<textarea id="c" readonly style="width:100%;height:90px;font-size:15px"></textarea>
<p><button onclick="navigator.clipboard.writeText(document.getElementById('c').value).then(()=>this.textContent='복사했습니다')"
 style="font-size:16px;padding:8px 16px">코드 복사</button>
 &nbsp;<a href="{actions}">카카오 연결 작업 열기 →</a></p>
<script>document.getElementById('c').value=new URLSearchParams(location.search).get('code')||'(코드가 없습니다. 카카오 로그인부터 다시 하세요)';</script>
</body></html>""", encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true", help="초안도 미리보기로 포함")
    ap.add_argument("--base-url", help="주소를 임시로 바꿔 만들기 (화면 확인용)")
    args = ap.parse_args()
    built = build(args.preview, args.base_url)
    for i in built:
        print(f"VOL.{i['vol']} ({i['status']}) → site/{i['rel']}/  ·  문안: {i['dir'].relative_to(ROOT)}/out/")
