"""
주간교육뉴스 — 카드 사진 만들기 (AI 이미지, 무료)

- Cloudflare Workers AI 무료 등급(하루 10,000 뉴런)의 FLUX.1-schnell 모델로 기사마다 사진을 만든다.
  한 장에 약 60 뉴런이라 매주 10장(선택 5 + 예비 5)을 만들어도 무료 한도의 1% 안팎이다.
- 필요한 비밀 값(GitHub Secrets): CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_API_TOKEN
  없으면 사진을 건너뛰고 카드는 분류별 그림판으로 나온다.
- 그림 설명(image_prompt)은 AI가 draft.yml 에 쓴다. 검토자가 설명을 고치면 파일 이름이 바뀌어 다음 실행 때 새로 만든다.
- 사람이 직접 고른 사진은 draft.yml 의 기사 항목에 image: 로 적으면 그것이 우선한다.
- 사진 사용을 허락받은 언론사(config/sources.csv 의 '사진허락' 이 Y)의 기사는
  그 기사의 대표 사진을 가져와 '사진: 언론사' 표시와 함께 쓴다. 허락받지 않은 언론사 사진은 절대 가져오지 않는다.

사진 우선순위:  사람이 지정한 image  →  허락받은 언론사의 원래 사진  →  AI 사진  →  (없으면 그림판)
"""
import base64
import csv
import hashlib
import io
import os
import re
import time
from pathlib import Path

import requests
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
UA = "Mozilla/5.0 (compatible; CSERC-NewsBot/1.0; +http://www.cserc.or.kr)"


def permitted_sources():
    """사진 사용을 허락받은 언론사 이름 목록"""
    out = set()
    with open(ROOT / "config/sources.csv", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if (r.get("사진허락") or "N").strip().upper() == "Y":
                out.add(r["이름"].strip())
    return out


def original_path(issue_dir: Path, item: dict) -> Path:
    return issue_dir / "images" / f"{item.get('cand', 'x')}-orig.jpg"


def fetch_original_photo(item: dict, out: Path) -> bool:
    """기사 페이지의 대표 사진(og:image)을 받아 저장한다. 허락받은 언론사에만 쓴다."""
    try:
        html = requests.get(item["link"], headers={"User-Agent": UA}, timeout=25).text
        m = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html)
             or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image', html))
        if not m:
            return False
        img = requests.get(m.group(1).replace("&amp;", "&"), headers={"User-Agent": UA, "Referer": item["link"]},
                           timeout=25)
        im = Image.open(io.BytesIO(img.content)).convert("RGB")
        if im.width < 500:  # 너무 작은 사진(로고 등)은 쓰지 않는다
            return False
        hist = im.convert("L").resize((200, 120)).histogram()
        if sum(hist[236:]) / sum(hist) > 0.45:  # 흰 바탕이 대부분 = 표·문서 캡처 → 카드에 안 맞으므로 AI 사진으로
            print(f"  원래 사진이 표·문서 이미지라 건너뜀 ({item.get('source')})")
            return False
        im.thumbnail((1400, 1400))
        out.parent.mkdir(parents=True, exist_ok=True)
        im.save(out, "JPEG", quality=86, optimize=True)
        return True
    except Exception as ex:
        print(f"  ! 원래 사진을 가져오지 못함 ({item.get('source')}): {type(ex).__name__}")
        return False


def choose(issue_dir: Path, item: dict):
    """카드에 쓸 사진을 고른다 → (파일 경로 또는 주소, 종류, 사진 표시 문구). 없으면 (None, None, '')"""
    own = item.get("image")
    if own:
        src = own if str(own).startswith("http") else issue_dir / own
        return src, "own", item.get("image_credit", "")
    orig = original_path(issue_dir, item)
    if orig.exists():
        return orig, "orig", f"사진: {item.get('source', '')}"
    gen = image_path(issue_dir, item)
    if gen and gen.exists():
        return gen, "ai", "AI 생성 이미지"
    return None, None, ""

MODEL = "@cf/black-forest-labs/flux-1-schnell"
STYLE = ("Photorealistic editorial news photograph, natural soft light, realistic Korean setting, "
         "documentary style, 35mm lens. {scene} "
         "No text, no letters, no numbers, no signs, no watermark, no logos, no famous people.")


def image_path(issue_dir: Path, item: dict) -> Path | None:
    prompt = (item.get("image_prompt") or "").strip()
    if not prompt:
        return None
    h = hashlib.md5(prompt.encode()).hexdigest()[:6]
    return issue_dir / "images" / f"{item.get('cand', 'x')}-{h}.jpg"


def available():
    return bool(os.environ.get("CLOUDFLARE_ACCOUNT_ID") and os.environ.get("CLOUDFLARE_API_TOKEN"))


def generate(prompt: str, out: Path, seed: int = 7) -> bool:
    url = (f"https://api.cloudflare.com/client/v4/accounts/{os.environ['CLOUDFLARE_ACCOUNT_ID']}"
           f"/ai/run/{MODEL}")
    body = {"prompt": STYLE.format(scene=prompt), "steps": 4, "seed": seed}
    for attempt in range(3):
        try:
            r = requests.post(url, json=body, timeout=120,
                              headers={"Authorization": f"Bearer {os.environ['CLOUDFLARE_API_TOKEN']}"})
            if r.status_code == 200:
                data = r.json()["result"]["image"]
                img = Image.open(io.BytesIO(base64.b64decode(data))).convert("RGB")
                out.parent.mkdir(parents=True, exist_ok=True)
                img.save(out, "JPEG", quality=86, optimize=True, progressive=True)
                return True
            print(f"  ! 사진 생성 실패 HTTP {r.status_code}: {r.text[:160]}")
            if r.status_code in (429, 500, 502, 503):
                time.sleep(10 * (attempt + 1))
                continue
            return False
        except requests.RequestException as ex:
            print(f"  ! 사진 생성 오류: {ex}")
            time.sleep(5)
    return False


def ensure_images(issue_dir: Path, draft: dict) -> int:
    """선택·예비 기사 중 사진이 없는 것만 채운다. (허락받은 언론사 원래 사진 → AI 사진) 채운 장수를 돌려준다."""
    items = list(draft.get("picks", [])) + list(draft.get("alternates", []))
    allowed = permitted_sources()
    made = 0
    for item in items:  # 1) 허락받은 언론사의 원래 사진
        if item.get("image") or item.get("source") not in allowed:
            continue
        out = original_path(issue_dir, item)
        if not out.exists() and fetch_original_photo(item, out):
            made += 1
    if not available():
        print("Cloudflare 설정이 없어 AI 사진은 건너뜁니다. (사진이 없는 카드에는 그림판이 들어감)")
        return made
    for item in items:  # 2) 나머지는 AI 사진
        if item.get("image") or original_path(issue_dir, item).exists():
            continue
        path = image_path(issue_dir, item)
        if path and not path.exists():
            print(f"  사진 만드는 중: {item.get('cand')} — {item['image_prompt'][:60]}")
            if generate(item["image_prompt"], path, seed=int(item.get("cand", 7))):
                made += 1
    return made
