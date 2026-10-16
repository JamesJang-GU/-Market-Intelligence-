# -*- coding: utf-8 -*-
"""
모든 Market Intelligence 크롤러를 순차 실행하고,
DB 갱신 완료 후 dashboard_generator.py를 실행하여 최신 HTML을 생성하는 통합 실행 파일

GitHub Actions 실행 예:
    python -u run_all.py
"""

import os
import sys
import subprocess
from pathlib import Path
from datetime import datetime

# =========================================================
# 1. 기본 설정
# =========================================================
BASE_DIR = Path(__file__).resolve().parent

# 각 작업의 기본 최대 실행시간(초) - 20분
DEFAULT_TIMEOUT_SECONDS = 20 * 60

# =========================================================
# 2. 실행 대상 설정 (실제 저장소 폴더/파일명 기준)
# =========================================================
CRAWLERS = [
    {
        "name": "Alphaliner",
        "path": BASE_DIR / "Alphaliner" / "alphaliner.py",
        "args": [],
        "timeout": 20 * 60,
    },
    {
        "name": "Bunker Price",
        "path": BASE_DIR / "BunkerPrice" / "Bunker Price.py",
        "args": ["--crawl-only"],  # Flask 서버 실행 방지
        "timeout": 20 * 60,
    },
    {
        "name": "Forwarder Top 25",
        "path": BASE_DIR / "Forwarder" / "Top25_Forwarder.py",
        "args": [],
        "timeout": 20 * 60,
    },
    {
        "name": "GDP & Inflation",
        "path": BASE_DIR / "GDP&Inflation" / "GDP_Inflation_GSCPI_DB.py",
        "args": [],
        "timeout": 20 * 60,
    },
    {
        "name": "Shipping Index (SCFI & BDI)",
        "path": BASE_DIR / "Shipping Index" / "ShippingIndex_SCFI&BDI.py",
        "args": [],
        "timeout": 20 * 60,
    },
    {
        "name": "TAC Index (Air Freight)",
        "path": BASE_DIR / "TAC Index" / "TAC_Index.py",
        "args": [],
        "timeout": 20 * 60,
    },
]

# 크롤링 완료 후 HTML 대시보드를 생성하는 후속 작업
DASHBOARD_GENERATOR = {
    "name": "Dashboard HTML Generator",
    "path": BASE_DIR / "dashboard_generator.py",
    "args": [],
    "timeout": 10 * 60,
}

# =========================================================
# 3. 보조 함수
# =========================================================
def now_string():
    """현재 시간을 로그 형식으로 반환"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def print_line():
    print("=" * 72, flush=True)

def run_task(task):
    """
    단일 파이썬 스크립트(크롤러 또는 생성기)를 실행하고 결과를 반환합니다.

    반환값:
        dict: name, status, message
    """
    name = task["name"]
    script_path = task["path"]
    extra_args = task.get("args", [])
    timeout_seconds = task.get("timeout", DEFAULT_TIMEOUT_SECONDS)

    print_line()
    print(f"[{now_string()}] ▶ START: {name}", flush=True)
    print(f"[INFO] Script : {script_path}", flush=True)
    print(f"[INFO] Timeout: {timeout_seconds // 60} minutes", flush=True)
    print_line()

    # 파일이 없으면 전체 작업을 즉시 중단하지 않고 SKIPPED 처리
    if not script_path.exists():
        message = f"File not found: {script_path}"
        print(f"[{now_string()}] ❌ SKIPPED: {name}", flush=True)
        print(f"[ERROR] {message}", flush=True)
        return {
            "name": name,
            "status": "SKIPPED",
            "message": message,
        }

    # -u 옵션: GitHub Actions 로그에서 실시간 버퍼링 없이 출력
    command = [
        sys.executable,
        "-u",
        str(script_path),
        *extra_args,
    ]

    env = os.environ.copy()

    try:
        result = subprocess.run(
            command,
            cwd=BASE_DIR,
            env=env,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )

        if result.returncode == 0:
            print_line()
            print(f"[{now_string()}] ✅ COMPLETED: {name}", flush=True)
            print_line()
            return {
                "name": name,
                "status": "SUCCESS",
                "message": "Completed successfully",
            }

        message = f"Process exited with code {result.returncode}"
        print_line()
        print(f"[{now_string()}] ❌ FAILED: {name}", flush=True)
        print(f"[ERROR] {message}", flush=True)
        print_line()
        return {
            "name": name,
            "status": "FAILED",
            "message": message,
        }

    except subprocess.TimeoutExpired:
        message = f"Exceeded timeout limit ({timeout_seconds // 60} minutes)"
        print_line()
        print(f"[{now_string()}] ⏰ TIMEOUT: {name}", flush=True)
        print(f"[ERROR] {message}", flush=True)
        print_line()
        return {
            "name": name,
            "status": "TIMEOUT",
            "message": message,
        }

    except Exception as error:
        message = f"{type(error).__name__}: {error}"
        print_line()
        print(f"[{now_string()}] ❌ EXCEPTION: {name}", flush=True)
        print(f"[ERROR] {message}", flush=True)
        print_line()
        return {
            "name": name,
            "status": "FAILED",
            "message": message,
        }

# =========================================================
# 4. 메인 실행부
# =========================================================
def main():
    start_time = datetime.now()
    results = []

    print()
    print("=" * 72, flush=True)
    print(" MARKET INTELLIGENCE - PIPELINE START", flush=True)
    print(f" Started at: {start_time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f" Base dir  : {BASE_DIR}", flush=True)
    print("=" * 72, flush=True)
    print()

    # 1단계: 모든 카테고리 크롤러 순차 실행 (DB 수집/갱신)
    print(f"[{now_string()}] [PHASE 1] Running {len(CRAWLERS)} Crawlers...", flush=True)
    for crawler in CRAWLERS:
        result = run_task(crawler)
        results.append(result)

    # 2단계: 최신 DB 기반으로 HTML 대시보드 렌더링
    print()
    print(f"[{now_string()}] [PHASE 2] Generating HTML Dashboard...", flush=True)
    dashboard_result = run_task(DASHBOARD_GENERATOR)
    results.append(dashboard_result)

    end_time = datetime.now()
    elapsed = end_time - start_time

    # -----------------------------------------------------
    # 최종 결과 요약
    # -----------------------------------------------------
    print()
    print("=" * 72, flush=True)
    print(" PIPELINE EXECUTION - FINAL SUMMARY", flush=True)
    print("=" * 72, flush=True)

    success_count = 0
    fail_count = 0

    for result in results:
        status = result["status"]
        name = result["name"]
        message = result["message"]

        if status == "SUCCESS":
            icon = "✅"
            success_count += 1
        elif status == "SKIPPED":
            icon = "⚠️"
            fail_count += 1
        elif status == "TIMEOUT":
            icon = "⏰"
            fail_count += 1
        else:
            icon = "❌"
            fail_count += 1

        print(f"{icon} [{status:<7}] {name} - {message}", flush=True)

    print("-" * 72, flush=True)
    print(f"Completed at : {end_time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"Elapsed time : {elapsed}", flush=True)
    print(f"Success      : {success_count}", flush=True)
    print(f"Failed/Skip  : {fail_count}", flush=True)
    print("=" * 72, flush=True)

    # 하나라도 실패/건너뜀이 발생하면 비정상 종료(exit code 1)하여 GitHub Actions에 알림
    if fail_count > 0:
        print("[RESULT] One or more tasks failed, timed out, or were skipped.", flush=True)
        sys.exit(1)

    print("[RESULT] All tasks (Crawling & HTML Generation) completed successfully.", flush=True)
    sys.exit(0)

if __name__ == "__main__":
    main()
