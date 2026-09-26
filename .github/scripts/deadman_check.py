#!/usr/bin/env python3
"""싼타임 외부 데드맨 감시 — 데몬 밖(GitHub Actions cron)에서 생존 신호를 본다.

입력(배포 repo 루트):
  heartbeat.json  {"ts": ISO8601, "added": n, "active": n}  — 소스 publish() 가 매 사이클 갱신·커밋
  deals.json      전체 딜 원장(created_iso 로 일별 발행 수 집계)

규칙:
  1) 무응답: heartbeat 나이 >= STALE_MIN(150분). 크롤은 매시간 랜덤 분이라 정상 간격 최대 약 2h + 실행시간.
     재알림은 6시간 버킷마다 1회(캐시 키 = ts + 버킷).
  2) 발행 급감: KST 09시대 실행에서 어제 발행 수 < 직전 7일 평균의 40%(평균 5건 미만이면 판정 안 함).
     하루 1회(캐시 키 = 날짜).
  3) push 트리거 감시자(deadman-watch.yml): GitHub cron 은 실측 3~4시간 간격으로만 돌아(2026-09-26) 30분 감시가
     안 된다. 그래서 heartbeat 커밋(push) 자체가 감시자를 깨운다 — `--sleep-until-stale` 로 ts+STALE_MIN 까지 잘 시간을
     계산해 자고, 깨어나 다시 판정. 다음 heartbeat push 가 오면 concurrency 가 이전 감시자를 취소한다.
출력(GITHUB_OUTPUT): alert=true|false, key=<dedupe key>, payload_file=<slack json path>, sleep_s=<초>
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
STALE_MIN = int(os.environ.get("STALE_MIN") or 150)  # 빈 문자열(push 이벤트) → 기본값
IS_TEST = bool(os.environ.get("STALE_MIN"))
REALERT_BUCKET_MIN = 360
VOLUME_HOUR_KST = 9
VOLUME_RATIO = 0.40
VOLUME_MIN_AVG = 5

ROOT = Path(os.environ.get("SITE_ROOT", "."))
RUNBOOK = (
    "확인: 맥에서 `bash scripts/daemon_ctl.sh status` → 중지면 GUI 터미널에서 "
    "`bash scripts/daemon_ctl.sh start` · 로그 `logs/daemon.log` 마지막 줄 확인"
)


def _out(alert: bool, key: str = "", text: str = "") -> None:
    payload_file = ""
    if alert:
        payload_file = str(Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "slack_payload.json")
        Path(payload_file).write_text(json.dumps({"text": text}, ensure_ascii=False), encoding="utf-8")
    lines = [f"alert={'true' if alert else 'false'}", f"key={key}", f"payload_file={payload_file}"]
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    if alert:
        print("---\n" + text)


def _fmt_kst(dt: datetime) -> str:
    return dt.astimezone(KST).strftime("%m-%d %H:%M KST")


def check_heartbeat(now: datetime) -> tuple[bool, str, str]:
    path = ROOT / "heartbeat.json"
    if not path.exists():
        print("heartbeat.json 없음 — 소스 배포 전이면 정상, 감시 보류")
        return False, "", ""
    ts = datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["ts"])
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_min = (now - ts).total_seconds() / 60
    print(f"heartbeat {ts.isoformat()} · 나이 {age_min:.0f}분")
    if age_min < STALE_MIN:
        return False, "", ""
    bucket = int(age_min // REALERT_BUCKET_MIN)
    h, m = divmod(int(age_min), 60)
    label = f"🧪 [테스트 · 기준 {STALE_MIN}분] " if IS_TEST else ""
    text = (
        f"{label}🚨 싼타임 데몬 무응답 — 마지막 생존 신호 {_fmt_kst(ts)} ({h}시간 {m}분 전). "
        f"매시간 크롤이 {STALE_MIN}분 넘게 사이트를 갱신하지 않았습니다.\n{RUNBOOK}"
    )
    prefix = "test-" if IS_TEST else ""  # 테스트가 실제 경보의 dedupe 키를 소모하지 않게
    return True, f"{prefix}deadman-{ts.strftime('%Y%m%dT%H%M%S')}-b{bucket}", text


def check_volume(now: datetime) -> tuple[bool, str, str]:
    now_kst = now.astimezone(KST)
    if now_kst.hour < VOLUME_HOUR_KST:
        return False, "", ""
    path = ROOT / "deals.json"
    if not path.exists():
        return False, "", ""
    counts: Counter[str] = Counter()
    for deal in json.loads(path.read_text(encoding="utf-8")):
        raw = deal.get("created_iso")
        if not raw:
            continue
        t = datetime.fromisoformat(raw)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        counts[t.astimezone(KST).date().isoformat()] += 1
    yday = (now_kst.date() - timedelta(days=1)).isoformat()
    prev = [counts[(now_kst.date() - timedelta(days=d)).isoformat()] for d in range(2, 9)]
    avg = sum(prev) / len(prev)
    y = counts[yday]
    print(f"어제({yday}) 발행 {y}건 · 직전 7일 평균 {avg:.1f}건 · 7일 {prev}")
    if avg < VOLUME_MIN_AVG or y >= avg * VOLUME_RATIO:
        return False, "", ""
    text = (
        f"⚠️ 싼타임 발행 급감 — 어제({yday}) {y}건, 직전 7일 평균 {avg:.0f}건의 {y / avg:.0%}. "
        "크롤은 돌지만 파싱·매칭이 조용히 망가졌을 수 있습니다(퀘존 2026-09-02 유형). "
        "연휴·주말 영향인지 먼저 확인하세요. 로그: `logs/daemon.log` 의 실행 요약 표(사이트별 크롤/신규/매칭)."
    )
    return True, f"volume-{yday}", text


def _heartbeat_ts() -> datetime | None:
    path = ROOT / "heartbeat.json"
    if not path.exists():
        return None
    ts = datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["ts"])
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def sleep_until_stale(now: datetime) -> int:
    """heartbeat 가 STALE_MIN 살이 되는 시각까지 남은 초(+2분 여유). 파일 없으면 0."""
    ts = _heartbeat_ts()
    if ts is None:
        return 0
    deadline = ts + timedelta(minutes=STALE_MIN + 2)
    remain = max(0, int((deadline - now).total_seconds()))
    print(f"heartbeat {ts.isoformat()} → {deadline.astimezone(KST):%m-%d %H:%M KST} 까지 {remain}s 대기")
    return remain


def main() -> int:
    now = datetime.now(timezone.utc)
    if "--sleep-until-stale" in sys.argv[1:]:
        remain = sleep_until_stale(now)
        gh_out = os.environ.get("GITHUB_OUTPUT")
        if gh_out:
            with open(gh_out, "a", encoding="utf-8") as fh:
                fh.write(f"sleep_s={remain}\n")
        print(f"sleep_s={remain}")
        return 0
    if os.environ.get("TEST_ALERT") == "true":
        run_id = os.environ.get("GITHUB_RUN_ID", "local")
        _out(True, f"test-{run_id}", f"🧪 싼타임 데드맨 감시 테스트 — {_fmt_kst(now)} 워크플로에서 Slack 연결 확인용")
        return 0
    for checker in (check_heartbeat, check_volume):
        alert, key, text = checker(now)
        if alert:
            _out(True, key, text)
            return 0
    _out(False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
