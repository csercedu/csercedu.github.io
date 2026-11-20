"""
주간교육뉴스 — 3단계: 카드뉴스 이미지 만들기

issues/<호>/draft.yml 을 읽어 1080x1080 카드 이미지를 만든다.
  01-cover.jpg   표지 (날짜, VOL, 해시태그, 기사 5건 썸네일)
  02~06-news.jpg 기사 카드 5장
  07-events.jpg  다가오는 연구소 행사 (draft.yml 의 events 에 내용이 있을 때만)

사진 자리
  - draft.yml 의 기사 항목에 image: (파일 경로 또는 이미지 주소)를 적으면 그 사진을 쓴다.
  - 없으면 분류별 색과 그림 문자로 만든 그림판을 자동으로 넣는다. (무료·저작권 걱정 없음)

실행:  python scripts/render_cards.py                 (가장 최근 호)
       python scripts/render_cards.py issues/2026-11-20-vol227
"""
import html
import sys
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
import images  # noqa: E402  AI 사진 만들기

ROOT = Path(__file__).resolve().parent.parent

# 분류별 그림판 색(위→아래)과 기본 그림 문자
CATEGORY_ART = {
    "입시·평가": ("#5b7db1", "#34507e", "📝"),
    "교육정책": ("#4f8576", "#2c5a4d", "🏛️"),
    "교사·교권": ("#c98243", "#94561f", "🧑‍🏫"),
    "학생·학부모": ("#b8668a", "#874063", "👨‍👩‍👧"),
    "학교현장": ("#6f9640", "#4b6c22", "🏫"),
    "기독교교육": ("#7d62ad", "#553d86", "📖"),
    "대안교육": ("#3f9470", "#24674a", "🌱"),
    "사학·종교": ("#9c7a45", "#6c5128", "⛪"),
    "인구·학교구조": ("#7f8790", "#535a62", "📉"),
    "디지털·AI": ("#2f86a6", "#1d5c75", "🤖"),
}
# 제목에 이런 말이 있으면 그림 문자를 바꾼다 (위에서부터 먼저 맞는 것)
EMOJI_HINTS = [
    (("교부금", "재정", "예산", "수당"), "💰"), (("부정행위", "커닝"), "🕵️"),
    (("정년", "연금"), "⏳"), (("대안학교", "대안교육"), "🌱"),
    (("스마트폰", "휴대전화", "폰 "), "📱"), (("수능", "시험", "평가", "내신", "입시"), "📝"),
    (("AI", "인공지능", "디지털", "코딩"), "🤖"), (("교권", "교사", "교원"), "🧑‍🏫"),
    (("학부모",), "👨‍👩‍👧"), (("돌봄", "초등"), "🏫"),
]


ACCENTS = {
    "입시·평가": ("✏️", "📚"), "교육정책": ("📜", "⚖️"), "교사·교권": ("📚", "🍎"),
    "학생·학부모": ("🎒", "💬"), "학교현장": ("🔔", "📚"), "기독교교육": ("✝️", "🕊️"),
    "대안교육": ("🌿", "🏡"), "사학·종교": ("⛪", "📖"), "인구·학교구조": ("👶", "🏫"), "디지털·AI": ("💻", "📱"),
}


def category_color(cat):
    return CATEGORY_ART.get(cat, ("#8a8a8a", "#5a5a5a", ""))[1]


def art_html(p, cls, emoji_size, label=True):
    """사진이 없을 때 넣는 그림판: 분류 색 + 큰 그림 문자 + 작은 보조 그림 문자 두 개"""
    top, bottom, emoji = CATEGORY_ART.get(p.get("category"), ("#8a8a8a", "#5a5a5a", "📰"))
    text = p.get("original_title", "") + " " + p.get("headline", "")
    for words, e in EMOJI_HINTS:
        if any(w in text for w in words):
            emoji = e
            break
    a1, a2 = ACCENTS.get(p.get("category"), ("📰", "🏫"))
    small = int(emoji_size * .42)
    lab = (f'<span class="label" style="font-size:{max(18, emoji_size // 4)}px">{html.escape(p.get("category", ""))}</span>'
           if label else "")
    return (f'<div class="visual art {cls}" style="background:radial-gradient(circle at 30% 20%,{top},{bottom} 75%)">'
            f'<span class="emoji" style="font-size:{small}px;position:absolute;left:14%;top:18%;transform:rotate(-14deg);opacity:.9">{a1}</span>'
            f'<span class="emoji" style="font-size:{emoji_size}px;position:relative">{emoji}</span>'
            f'<span class="emoji" style="font-size:{small}px;position:absolute;right:13%;bottom:16%;transform:rotate(12deg);opacity:.9">{a2}</span>'
            f'{lab}</div>')


def pick_image(p, issue_dir):
    """사진 우선순위는 images.choose 참고 → (주소, 사진 표시 문구)"""
    src, kind, credit = images.choose(issue_dir, p)
    if src is None:
        return None, ""
    return (src if isinstance(src, str) else Path(src).resolve().as_uri()), credit


def visual_html(p, cls, emoji_size, issue_dir, label=True):
    src, _ = pick_image(p, issue_dir)
    if src:
        return f'<div class="visual {cls}"><img class="photo" src="{html.escape(src)}"></div>'
    return art_html(p, cls, emoji_size, label)


def split_title(title, max_line=15):
    """원 기사 제목을 단어 단위로 2~4줄로 고르게 나눈다. [속보] 같은 머리표는 뺀다."""
    import re
    t = re.sub(r"^\s*(\[[^\]]{1,8}\]\s*)+", "", title).strip()
    words = t.split()
    n = max(1, min(4, -(-len(t) // max_line)))
    target = len(t) / n
    lines, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > target + 3 and len(lines) < n - 1:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    lines.append(cur)
    return lines


def headline_size(lines):
    longest = max(len(l) for l in lines)
    return min(64, int(910 / (longest + 0.8)))


def render(issue_dir: Path):
    draft = yaml.safe_load((issue_dir / "draft.yml").read_text(encoding="utf-8"))
    images.ensure_images(issue_dir, draft)
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=True)
    root = ROOT.resolve().as_uri()
    css = env.get_template("card.css").render(root=root)
    out = issue_dir / "cards"
    out.mkdir(exist_ok=True)
    for old in list(out.glob("*.png")) + list(out.glob("*.jpg")):
        old.unlink()

    mode = (yaml.safe_load((ROOT / "config/settings.yml").read_text(encoding="utf-8"))["issue"]
            .get("headline", "original"))
    picks = []
    for p in draft["picks"]:
        if mode == "original" and p.get("original_title"):
            lines = split_title(str(p["original_title"]))
        else:
            lines = [l.strip() for l in str(p["headline"]).strip().split("\n") if l.strip()]
        q = dict(p)
        q["headline_html"] = "<br>".join(html.escape(l) for l in lines)
        q["headline_size"] = headline_size(lines)
        q["cover_caption"] = " ".join(lines)
        q["visual_html"] = visual_html(p, "", 170, issue_dir, label=False)
        q["thumb_html"] = visual_html(p, "", 74, issue_dir)
        q["photo_credit"] = pick_image(p, issue_dir)[1]
        q["color"] = category_color(p.get("category"))
        q["points"] = p.get("points") or []
        if not q["points"] and p.get("highlight"):  # 핵심 세 줄이 없는 예전 초안: AI가 쓴 요약을 문장 단위로
            q["points"] = [x.strip() + "." for x in str(p.get("summary", "")).split(".") if x.strip()][:3]
        # 카드 밖으로 넘치지 않게 한 줄 40자로 자른다 (원문은 draft.yml 에 그대로)
        q["points"] = [t if len(t) <= 40 else t[:39].rstrip() + "…" for t in map(str, q["points"])]
        picks.append(q)

    pages = []
    y, m, d = str(draft["date"]).split("-")
    # 표지 칸 배치: 기사 수에 맞춰 12칸 격자를 나눈다 (5건 = 위 3칸·아래 2칸)
    layouts = {1: [12], 2: [6, 6], 3: [4, 4, 4], 4: [6, 6, 6, 6], 5: [4, 4, 4, 6, 6],
               6: [4] * 6, 7: [3, 3, 3, 3, 4, 4, 4]}
    spans = layouts.get(len(picks), [4] * len(picks))
    one_row = len(picks) <= 3
    cover_picks = []
    for i, q in enumerate(picks):
        row2 = not one_row and sum(spans[:i]) >= 12
        h = 300 if one_row else (132 if row2 else 150)
        cover_picks.append(dict(q, visual_html=q["thumb_html"], span=spans[i], thumb_h=h))
    pages.append(("01-cover.jpg", env.get_template("cover.html").render(
        css=css, root=root, vol=draft["vol"], date_dot=f"{y}.{m}.{d}",
        hashtags=draft.get("hashtags", []), picks=cover_picks)))
    for i, q in enumerate(picks, 2):
        pages.append((f"{i:02d}-news.jpg", env.get_template("article.html").render(css=css, root=root, p=q)))
    events = [e for e in (draft.get("events") or []) if e.get("title")]
    for e in events:
        if e.get("poster") and not str(e["poster"]).startswith("http"):
            e["poster"] = (issue_dir / e["poster"]).resolve().as_uri()
    if events:
        pages.append((f"{len(picks) + 2:02d}-events.jpg", env.get_template("event.html").render(
            css=css, root=root, events=events)))

    warnings = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chrome_path())
        page = browser.new_page(viewport={"width": 1080, "height": 1080}, device_scale_factor=1)
        for name, doc in pages:
            tmp = out / f"_{name}.html"
            tmp.write_text(doc, encoding="utf-8")
            page.goto(tmp.resolve().as_uri())
            page.evaluate("document.fonts.ready")
            page.wait_for_timeout(150)
            # 넘침 점검: 그림 자리가 너무 작아졌거나 글이 카드 밖으로 나가면 경고
            info = page.evaluate("""() => {
              const v = document.querySelector('.hero');
              const paper = document.querySelector('.paper').getBoundingClientRect();
              const last = [...document.querySelectorAll('.paper > *')].pop().getBoundingClientRect();
              return {visual: v ? Math.round(v.getBoundingClientRect().height) : null,
                      overflow: Math.round(last.bottom - paper.bottom)};
            }""")
            if info["visual"] is not None and info["visual"] < 300:
                warnings.append(f"{name}: 글이 길어 사진 자리가 {info['visual']}px로 줄었음 — 제목이나 핵심 세 줄을 줄이세요")
            if info["overflow"] > 2:
                warnings.append(f"{name}: 내용이 카드 밖으로 {info['overflow']}px 넘침")
            page.screenshot(path=str(out / name), type="jpeg", quality=92)
            tmp.unlink()
        browser.close()
    return [out / n for n, _ in pages], warnings


def chrome_path():
    """깃허브 서버에서는 Playwright 기본 크로뮴, 이 작업 환경에서는 설치된 크롬을 쓴다."""
    for p in ("/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"):
        if Path(p).exists():
            return p
    return None


def latest_issue():
    dirs = sorted(d for d in (ROOT / "issues").iterdir() if (d / "draft.yml").exists())
    return dirs[-1]


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_issue()
    files, warns = render(target.resolve())
    for f in files:
        print("만듦:", f.relative_to(ROOT))
    for w in warns:
        print("  ! " + w)
