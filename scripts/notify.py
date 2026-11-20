"""
주간교육뉴스 — 4단계: 보내기 (Gmail, 카카오톡 '나에게 보내기')

비밀 값은 모두 GitHub 저장소의 Secrets 에 넣고, 실행할 때 환경변수로 받는다. (코드·저장소에는 절대 적지 않음)
  GMAIL_ADDRESS        보내는 Gmail 주소
  GMAIL_APP_PASSWORD   Gmail '앱 비밀번호' (2단계 인증을 켠 뒤 만들 수 있음)
  EDITOR_EMAILS        검토자 메일 (쉼표로 여러 명)
  MAIL_RECIPIENTS      구독자 메일 목록 (한 줄에 하나, 또는 쉼표)
  KAKAO_REST_API_KEY   카카오 개발자 앱의 REST API 키
  KAKAO_CLIENT_SECRET  (앱에서 켠 경우에만)
  KAKAO_REFRESH_TOKEN  '카카오 연결' 작업으로 받은 토큰
  GH_PAT               (선택) 카카오 토큰이 갱신될 때 Secrets 를 자동으로 고쳐 쓰기 위한 토큰
"""
import json
import os
import re
import smtplib
import subprocess
import time
from email.headerregistry import Address
from email.message import EmailMessage
from pathlib import Path

import requests

DRY = os.environ.get("DRY_RUN") == "1"  # 1이면 실제로 보내지 않고 내용만 출력


# ---------------------------------------------------------------- 메일
def recipients(env_name):
    raw = os.environ.get(env_name, "")
    return [a.strip() for a in re.split(r"[\n,;]+", raw) if "@" in a]


def build_mail(to, subject, html, text, from_name, attachments=(), reply_to=None):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = Address(from_name, addr_spec=os.environ.get("GMAIL_ADDRESS", "sender@example.com"))
    msg["To"] = to
    if reply_to:
        msg["Reply-To"] = reply_to
        msg["List-Unsubscribe"] = f"<mailto:{reply_to}?subject=unsubscribe>"
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    for path in attachments:
        p = Path(path)
        maintype, subtype = {".png": ("image", "png"), ".jpg": ("image", "jpeg"), ".html": ("text", "html"),
                             ".txt": ("text", "plain")}.get(p.suffix, ("application", "octet-stream"))
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)
    return msg


def send_mails(messages):
    """messages: EmailMessage 목록. Gmail 한도를 고려해 한 통씩 천천히 보낸다."""
    if DRY or not os.environ.get("GMAIL_APP_PASSWORD"):
        for m in messages:
            print(f"[메일 미발송·시험] To={m['To']} | {m['Subject']}")
        return len(messages)
    sent = 0
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
        s.login(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"])
        for m in messages:
            try:
                s.send_message(m)
                sent += 1
            except smtplib.SMTPException as ex:
                print(f"  ! {m['To']} 발송 실패: {ex}")
            time.sleep(1.2)
    print(f"메일 {sent}/{len(messages)}통 발송")
    return sent


# ---------------------------------------------------------------- 카카오톡 나에게 보내기
def kakao_access_token():
    key = os.environ.get("KAKAO_REST_API_KEY")
    refresh = os.environ.get("KAKAO_REFRESH_TOKEN")
    if not (key and refresh):
        return None
    data = {"grant_type": "refresh_token", "client_id": key, "refresh_token": refresh}
    if os.environ.get("KAKAO_CLIENT_SECRET"):
        data["client_secret"] = os.environ["KAKAO_CLIENT_SECRET"]
    r = requests.post("https://kauth.kakao.com/oauth/token", data=data, timeout=20)
    js = r.json()
    if "access_token" not in js:
        raise RuntimeError(f"카카오 토큰 갱신 실패 — '카카오 연결' 작업을 다시 실행하세요: {js}")
    if js.get("refresh_token"):  # 만료 1개월 전부터는 새 토큰이 온다 → 저장해 두어야 계속 쓸 수 있다
        save_new_refresh_token(js["refresh_token"])
    return js["access_token"]


def save_new_refresh_token(token):
    repo, pat = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GH_PAT")
    if repo and pat:
        subprocess.run(["gh", "secret", "set", "KAKAO_REFRESH_TOKEN", "--repo", repo, "--body", token],
                       check=True, env={**os.environ, "GH_TOKEN": pat})
        print("카카오 토큰이 갱신되어 Secrets 에 새로 저장했습니다.")
    else:
        print("! 카카오 토큰이 갱신됐지만 GH_PAT 이 없어 저장하지 못했습니다. 한 달 안에 '카카오 연결'을 다시 실행하세요.")


def kakao_send_to_me(title, description, image_url, link, button="카드뉴스 보기"):
    feed = {
        "object_type": "feed",
        "content": {
            "title": title[:200], "description": description[:200],
            "image_url": image_url, "image_width": 1080, "image_height": 1080,
            "link": {"web_url": link, "mobile_web_url": link},
        },
        "buttons": [{"title": button, "link": {"web_url": link, "mobile_web_url": link}}],
    }
    if DRY:
        print("[카카오 미발송·시험]", json.dumps(feed, ensure_ascii=False)[:300])
        return False
    token = kakao_access_token()
    if not token:
        print("카카오 설정이 없어 '나에게 보내기'를 건너뜁니다.")
        return False
    r = requests.post("https://kapi.kakao.com/v2/api/talk/memo/default/send",
                      headers={"Authorization": f"Bearer {token}"},
                      data={"template_object": json.dumps(feed, ensure_ascii=False)}, timeout=20)
    ok = r.status_code == 200 and r.json().get("result_code") == 0
    print("카카오 나에게 보내기:", "성공" if ok else f"실패 {r.status_code} {r.text[:200]}")
    return ok
