"""
주간교육뉴스 — 전체 흐름 실행기 (GitHub Actions 가 이 파일을 부른다)

  python scripts/pipeline.py collect            매일 아침: 기사 수집 + 기사 고르기 화면
  python scripts/pipeline.py make --ids a1b2c3,d4e5f6 --issue 12
                                                편집자가 고른 기사로: AI 문안·사진 → 카드 → 미리보기 (요청 이슈에 댓글)
  python scripts/pipeline.py thursday           목요일 저녁: 아직 안 골랐으면 '기사를 골라 주세요' 알림
  python scripts/pipeline.py draft              (예비) AI가 대신 고르기: 수집 → AI 선별·요약 → 카드 → 미리보기
  python scripts/pipeline.py notify-review      (미리보기 페이지가 올라간 뒤) 검토 요청 메일·카톡
  python scripts/pipeline.py publish --trigger push|schedule|manual
                                                초안이 확정되면 발행 준비 (카드 → 페이지 → 문안)
  python scripts/pipeline.py notify-published   (페이지가 올라간 뒤) 구독자 메일·카톡·수동 채널 자료 발송

상태 흐름:  draft(초안) → confirmed(확정) → published(발행)
  - 검토자는 GitHub 에서 draft.yml 을 열어 고치고, status 를 confirmed 로 바꿔 저장하면 바로 발행된다.
  - settings.yml 의 review.require_confirmation 이 false 이면, 금요일 오전까지 응답이 없을 때 AI 초안 그대로 발행된다.
"""
import argparse
import html
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify  # noqa: E402
from build_site import week_label  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
KST = timezone(timedelta(hours=9))
PENDING = ROOT / ".notify.yml"  # 페이지가 올라간 뒤 보낼 알림 내용을 잠시 적어 두는 곳


def run(*args):
    print("▶", " ".join(args), flush=True)
    subprocess.run([sys.executable, *args], cwd=ROOT, check=True)


def settings():
    return yaml.safe_load((ROOT / "config/settings.yml").read_text(encoding="utf-8"))


def latest_issue_dir():
    dirs = sorted(d for d in (ROOT / "issues").iterdir() if (d / "draft.yml").exists())
    return dirs[-1] if dirs else None


def load(d):
    return yaml.safe_load((d / "draft.yml").read_text(encoding="utf-8"))


def set_status(d, status):
    """draft.yml 의 주석과 서식은 그대로 두고 status 줄만 바꾼다."""
    f = d / "draft.yml"
    lines = f.read_text(encoding="utf-8").splitlines()
    for n, line in enumerate(lines):
        if line.startswith("status:"):
            lines[n] = f"status: {status}"
            break
    if status == "published":
        lines.insert(n + 1, f"published_at: '{datetime.now(KST).isoformat(timespec='minutes')}'")
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")


def bump_last_vol(vol):
    f = ROOT / "config/settings.yml"
    text = f.read_text(encoding="utf-8")
    import re
    f.write_text(re.sub(r"last_vol: \d+", f"last_vol: {vol}", text, count=1), encoding="utf-8")


def issue_url(d, y, preview):
    base = settings()["site"]["base_url"].rstrip("/")
    return f"{base}/{'preview/' if preview else ''}vol{y['vol']}/"


def edit_url(d):
    repo = os.environ.get("GITHUB_REPOSITORY", "OWNER/REPO")
    return f"https://github.com/{repo}/edit/main/{d.relative_to(ROOT).as_posix()}/draft.yml"


def headline_line(p):
    return " ".join(l.strip() for l in str(p["headline"]).split("\n") if l.strip())


# ---------------------------------------------------------------- 단계
def cmd_collect():
    run("scripts/collect.py", "--days", str(settings()["issue"]["days"]), "--top", "60")
    run("scripts/build_site.py", "--preview")  # 기사 고르기 화면(site/review/) 갱신


def cmd_make(ids, issue):
    from ai_select import next_issue
    s = settings()
    args = ["scripts/ai_select.py", "--ids", ids]
    if issue:
        args += ["--request-issue", str(issue)]
    run(*args)
    _, _, d = next_issue(s)
    if not (d / "draft.yml").exists():
        sys.exit("카드 문안을 만들지 못했습니다. GEMINI_API_KEY 설정을 확인하세요.")
    run("scripts/render_cards.py", str(d))
    run("scripts/build_site.py", "--preview")
    PENDING.write_text(yaml.safe_dump({"kind": "made", "dir": str(d.relative_to(ROOT)), "issue": issue},
                                      allow_unicode=True))


def cmd_thursday():
    from ai_select import next_issue
    _, vol, d = next_issue(settings())
    if (d / "draft.yml").exists():
        print(f"VOL.{vol} 초안이 이미 있습니다. 알림을 보내지 않습니다.")
        return
    PENDING.write_text(yaml.safe_dump({"kind": "remind-select", "vol": vol}, allow_unicode=True))


def cmd_draft():
    s = settings()
    run("scripts/collect.py", "--days", str(s["issue"]["days"]), "--top", "60")
    run("scripts/ai_select.py", *(["--force"] if os.environ.get("FORCE_DRAFT") == "1" else []))
    from ai_select import next_issue
    _, _, d = next_issue(s)
    if not (d / "draft.yml").exists():
        sys.exit("AI 초안을 만들지 못했습니다. GEMINI_API_KEY 설정을 확인하거나, 한도가 풀린 뒤 "
                 "'2. 목요일 초안 만들기'를 다시 실행하세요. (지시문은 issues/ 폴더의 prompt.txt 에 저장됨)")
    run("scripts/render_cards.py", str(d))
    run("scripts/build_site.py", "--preview")
    PENDING.write_text(yaml.safe_dump({"kind": "review", "dir": str(d.relative_to(ROOT))}, allow_unicode=True))


def cmd_publish(trigger):
    s = settings()
    d = latest_issue_dir()
    y = load(d)
    status = y["status"]
    print(f"VOL.{y['vol']} 현재 상태: {status} (실행 계기: {trigger})")
    if status == "published":
        print("이미 발행된 호입니다. 페이지만 다시 만듭니다.")
        run("scripts/build_site.py", "--preview")
        return
    go = (status == "confirmed" or trigger in ("manual", "comment")
          or (trigger == "schedule" and not s["review"]["require_confirmation"]))
    if not go:
        run("scripts/render_cards.py", str(d))  # 고친 내용을 미리보기에 반영
        run("scripts/build_site.py", "--preview")
        if trigger == "schedule":  # 금요일 오전인데 아직 확정 전
            PENDING.write_text(yaml.safe_dump({"kind": "remind", "dir": str(d.relative_to(ROOT))}, allow_unicode=True))
        return
    run("scripts/render_cards.py", str(d))
    set_status(d, "published")
    bump_last_vol(y["vol"])
    run("scripts/build_site.py", "--preview")
    PENDING.write_text(yaml.safe_dump({"kind": "published", "dir": str(d.relative_to(ROOT)),
                                       "issue": y.get("request_issue")}, allow_unicode=True))


# ---------------------------------------------------------------- 알림
def issue_comment(issue, body, close=False):
    """만들기 요청 이슈에 댓글을 단다 (GitHub 실행 중에만)"""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not (issue and repo and os.environ.get("GH_TOKEN")):
        print(f"[이슈 댓글 미발송·시험] #{issue}\n{body[:400]}")
        return
    subprocess.run(["gh", "issue", "comment", str(issue), "--repo", repo, "--body", body], check=False)
    if close:
        subprocess.run(["gh", "issue", "close", str(issue), "--repo", repo], check=False)


def review_url():
    return settings()["site"]["base_url"].rstrip("/") + "/review/"


def made_comment(d, y, url):
    rows = []
    for p in y["picks"]:
        checks = "".join(f"\n   - ⚠️ {c}" for c in p.get("check", []))
        rows.append(f"{p['order']}. **{headline_line(p)}** — {p['source']}{checks}")
    return (f"### VOL.{y['vol']} 카드뉴스 시안이 만들어졌습니다\n\n"
            f"**[미리보기 열기]({url})**\n\n![표지]({url}01-cover.jpg)\n\n" + "\n".join(rows) +
            f"\n\n**발행하려면** 이 글에 `발행` 이라고 댓글을 다세요. 약 3분 뒤 구독자 메일까지 나갑니다.\n\n"
            f"**고치려면** [초안 고치기]({edit_url(d)})에서 문구·그림 설명(`image_prompt`)을 고쳐 저장하면 미리보기가 다시 만들어집니다. "
            f"기사를 바꾸려면 [기사 고르기 화면]({review_url()})에서 다시 골라 새 요청을 만드세요.")
def review_html(d, y, url, auto):
    rows = []
    for p in y["picks"]:
        checks = "".join(f"<li style='color:#b3261e'>{html.escape(c)}</li>" for c in p.get("check", []))
        rows.append(f"<li style='margin:10px 0'><b>{html.escape(headline_line(p))}</b> — {html.escape(p['source'])}"
                    f"<br><span style='color:#555'>{html.escape(p.get('why', ''))}</span>"
                    f"{'<ul>' + checks + '</ul>' if checks else ''}</li>")
    alts = "".join(f"<li>{html.escape(headline_line(a))} — {html.escape(a['source'])}</li>" for a in y.get("alternates", []))
    deadline = ("금요일 오전 10시까지 확정하지 않으면 <b>이 초안 그대로 자동 발행</b>됩니다." if auto
                else "확정해야 발행됩니다. (현재 '확인 필수' 기간)")
    return f"""<div style="font-family:'Malgun Gothic',sans-serif;line-height:1.6;max-width:640px">
<h2 style="margin:0 0 6px">주간교육뉴스 VOL.{y['vol']} 초안 검토 요청</h2>
<p>AI가 고른 기사입니다. {deadline}</p>
<p><a href="{url}" style="background:#161616;color:#fff;padding:9px 16px;text-decoration:none;font-weight:700">미리보기 열기</a>
&nbsp; <a href="{edit_url(d)}" style="background:#e8603c;color:#fff;padding:9px 16px;text-decoration:none;font-weight:700">고치기·확정하기</a></p>
<h3>실을 기사 5건</h3><ol>{''.join(rows)}</ol>
<h3>예비 기사</h3><ul>{alts}</ul>
<h3>확정하는 법</h3>
<ol><li>'고치기·확정하기'를 누르면 GitHub에서 초안 파일이 열립니다.</li>
<li>바꿀 문장이 있으면 고치고, 기사를 바꾸려면 picks 항목을 지우고 alternates 항목을 옮겨 옵니다.</li>
<li>맨 위의 <code>status: draft</code> 를 <code>status: confirmed</code> 로 바꾼 뒤 <b>Commit changes</b> 를 누릅니다.
(기사 고르기 화면에서 요청했다면 그 요청 글에 <code>발행</code> 이라고 댓글을 달아도 됩니다)</li>
<li>약 3분 뒤 발행되고 구독자에게 메일이 나갑니다.</li></ol></div>"""


def manual_kit_html(y, url, d):
    kakao = (d / "out/kakao.txt").read_text(encoding="utf-8")
    board = (d / "out/board.txt").read_text(encoding="utf-8")
    return f"""<div style="font-family:'Malgun Gothic',sans-serif;line-height:1.6;max-width:640px">
<h2 style="margin:0 0 6px">VOL.{y['vol']} 발행 완료 — 손으로 올릴 곳 자료</h2>
<p>뉴스 페이지: <a href="{url}">{url}</a><br>구독자 메일은 자동으로 발송됐습니다.</p>
<h3>1. 카톡 단체방·오픈채팅방</h3>
<p>카카오톡 '나와의 채팅'에 온 카드를 길게 눌러 <b>전달</b> → 여러 방을 한 번에 선택하세요. 아래 글을 붙여 넣어도 됩니다.</p>
<pre style="background:#f4f2ed;padding:12px;white-space:pre-wrap">{html.escape(kakao)}</pre>
<h3>2. 카카오톡 채널 '소식'</h3>
<p>채널 관리자센터 → 소식 작성 → 첨부한 카드 이미지를 올리고 위 글을 붙여 넣으세요.</p>
<h3>3. 홈페이지 게시판(주간교육뉴스)</h3>
<pre style="background:#f4f2ed;padding:12px;white-space:pre-wrap">{html.escape(board)}</pre>
<h3>4. 홈페이지 뉴스레터</h3>
<p>첨부한 <b>email.html</b> 을 열어 전체 선택·복사한 뒤 뉴스레터 편집기에 붙여 넣으세요.</p></div>"""


def cmd_notify():
    if not PENDING.exists():
        print("보낼 알림이 없습니다.")
        return
    info = yaml.safe_load(PENDING.read_text(encoding="utf-8"))
    s = settings()
    if info["kind"] == "remind-select":  # 목요일 저녁: 아직 고른 기사가 없음
        body = (f"<div style=\"font-family:'Malgun Gothic',sans-serif;line-height:1.6\"><h2>주간교육뉴스 VOL.{info['vol']} 기사를 골라 주세요</h2>"
                f"<p>아직 이번 호 기사가 선택되지 않았습니다.</p><p><a href=\"{review_url()}\" "
                f"style=\"background:#e8603c;color:#fff;padding:9px 16px;text-decoration:none;font-weight:700\">기사 고르기 화면 열기</a></p>"
                f"<p>바쁘시면 GitHub의 Actions → '2. 목요일 알림 · AI에게 맡기기' → Run workflow 로 AI가 대신 고르게 할 수 있습니다.</p></div>")
        notify.send_mails([notify.build_mail(e, f"[주간교육뉴스] VOL.{info['vol']} 기사를 골라 주세요", body,
                                             f"기사 고르기: {review_url()}", s["site"]["org"])
                           for e in notify.recipients("EDITOR_EMAILS")])
        notify.kakao_send_to_me(f"VOL.{info['vol']} 기사를 골라 주세요", "이번 주 후보 기사가 준비돼 있습니다.",
                                s["site"]["base_url"].rstrip("/") + "/assets/emblem.png", review_url(), button="기사 고르기")
        PENDING.unlink()
        return
    d = ROOT / info["dir"]
    y = load(d)
    org = s["site"]["org"]
    editors = notify.recipients("EDITOR_EMAILS")
    contact = s["site"].get("contact") or os.environ.get("GMAIL_ADDRESS")
    titles = "\n".join(f"{n}. {headline_line(p)}" for n, p in enumerate(y["picks"], 1))

    if info["kind"] == "made":
        url = issue_url(d, y, preview=True)
        issue_comment(info.get("issue"), made_comment(d, y, url))
        notify.kakao_send_to_me(f"VOL.{y['vol']} 카드뉴스 시안이 준비됐습니다", titles, f"{url}01-cover.jpg", url,
                                button="미리보기")
        PENDING.unlink()
        return

    if info["kind"] in ("review", "remind"):
        url = issue_url(d, y, preview=True)
        auto = not s["review"]["require_confirmation"]
        prefix = "[확정 필요] " if info["kind"] == "remind" else ""
        body = review_html(d, y, url, auto)
        notify.send_mails([notify.build_mail(e, f"{prefix}주간교육뉴스 VOL.{y['vol']} 초안 검토 요청", body,
                                             f"미리보기: {url}\n고치기·확정: {edit_url(d)}\n\n{titles}", org)
                           for e in editors])
        notify.kakao_send_to_me(f"{prefix}VOL.{y['vol']} 초안이 준비됐습니다", titles, f"{url}01-cover.jpg", url,
                                button="미리보기")

    elif info["kind"] == "published":
        url = issue_url(d, y, preview=False)
        out = d / "out"
        subject = (out / "email_subject.txt").read_text(encoding="utf-8").strip()
        page = (out / "email.html").read_text(encoding="utf-8")
        text = (out / "kakao.txt").read_text(encoding="utf-8")
        if s.get("mail", {}).get("send_newsletter", True):
            subs = notify.recipients("MAIL_RECIPIENTS")
            notify.send_mails([notify.build_mail(r, subject, page, text, org, reply_to=contact) for r in subs])
        from datetime import date
        notify.kakao_send_to_me(f"주간교육뉴스 VOL.{y['vol']} | {week_label(date.fromisoformat(str(y['date'])))}",
                                titles, f"{url}01-cover.jpg", url)
        cards = sorted((d / "cards").glob("*.jpg"))
        notify.send_mails([notify.build_mail(e, f"[발행 완료] 주간교육뉴스 VOL.{y['vol']} — 수동 채널 자료",
                                             manual_kit_html(y, url, d), text, org,
                                             attachments=[*cards, out / "email.html"]) for e in editors])
        issue_comment(info.get("issue"), f"✅ VOL.{y['vol']} 발행 완료 — {url}\n\n구독자 메일을 보냈고, "
                      f"단체방·채널·게시판용 자료는 검토자 메일로 보냈습니다.", close=True)
    PENDING.unlink()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["collect", "make", "thursday", "draft", "notify", "notify-review",
                                        "publish", "notify-published"])
    ap.add_argument("--trigger", default="manual", choices=["push", "schedule", "manual", "comment"])
    ap.add_argument("--ids", help="make: 고른 후보 ID (쉼표)")
    ap.add_argument("--issue", type=int, help="make: 요청 이슈 번호")
    a = ap.parse_args()
    if a.command == "collect":
        cmd_collect()
    elif a.command == "make":
        cmd_make(a.ids, a.issue)
    elif a.command == "thursday":
        cmd_thursday()
    elif a.command == "draft":
        cmd_draft()
    elif a.command == "publish":
        cmd_publish(a.trigger)
    else:
        cmd_notify()
