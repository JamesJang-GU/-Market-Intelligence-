# -*- coding: utf-8 -*-
"""
모든 Market Intelligence 크롤러를 순차 실행하는 통합 실행 파일

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

# 각 크롤러의 최대 실행시간(초)
# 20분 = 1,200초
# 필요하면 크롤러별 timeout 값을 조정하세요.
DEFAULT_TIMEOUT_SECONDS = 20 * 60

# =========================================================
# 2. 실행 대상 설정
# =========================================================
# 실제 저장소의 파일 경로와 다르면 path 값만 수정하세요.
#
# Bunker Price.py는 Flask 서버가 실행되지 않도록 반드시
# "--crawl-only" 옵션을 사용합니다.
CRAWLERS = [
    {
        "name": "Bunker Price",
        "path": BASE_DIR / "BunkerPrice" / "Bunker Price.py",
        "args": ["--crawl-only"],
        "timeout": 20 * 60,
    },

    # 아래는 실제 파일이 존재할 때만 주석을 해제하거나
    # 경로를 현재 저장소 구조에 맞게 수정해서 사용하세요.

    # {
    #     "name": "Oil Price",
    #     "path": BASE_DIR / "OilPrice" / "Oil Price.py",
    #     "args": [],
    #     "timeout": 15 * 60,
    # },
    #
    # {
    #     "name": "Shipping Index",
    #     "path": BASE_DIR / "ShippingIndex" / "Shipping Index.py",
    #     "args": [],
    #     "timeout": 15 * 60,
    # },
    #
    # {
    #     "name": "Air Freight Index",
    #     "path": BASE_DIR / "Air" / "TAC Index.py",
    #     "args": [],
    #     "timeout": 15 * 60,
    # },
]

# =========================================================
# 3. 보조 함수
# =========================================================
def now_string():
    """현재 시간을 로그 형식으로 반환"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def print_line():
    print("=" * 72, flush=True)

def run_crawler(crawler):
    """
    크롤러 한 개를 실행하고 결과를 반환합니다.

    반환값:
        dict: name, status, message
    """
    name = crawler["name"]
    script_path = crawler["path"]
    extra_args = crawler.get("args", [])
    timeout_seconds = crawler.get("timeout", DEFAULT_TIMEOUT_SECONDS)

    print_line()
    print(f"[{now_string()}] ▶ START: {name}", flush=True)
    print(f"[INFO] Script : {script_path}", flush=True)
    print(f"[INFO] Timeout: {timeout_seconds // 60} minutes", flush=True)
    print_line()

    # 파일이 없으면 전체 작업을 멈추지 않고 실패 기록만 남김
    if not script_path.exists():
        message = f"File not found: {script_path}"
        print(f"[{now_string()}] ❌ SKIPPED: {name}", flush=True)
        print(f"[ERROR] {message}", flush=True)

        return {
            "name": name,
            "status": "SKIPPED",
            "message": message,
        }

    # python -u:
    # 출력 버퍼를 사용하지 않아 GitHub Actions 로그에 즉시 표시됨
    command = [
        sys.executable,
        "-u",
        str(script_path),
        *extra_args,
    ]

    # GitHub Actions 여부를 하위 프로세스에도 명확히 전달
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
    print(" MARKET INTELLIGENCE - ALL CRAWLERS START", flush=True)
    print(f" Started at: {start_time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f" Base dir  : {BASE_DIR}", flush=True)
    print("=" * 72, flush=True)
    print()

    for crawler in CRAWLERS:
        result = run_crawler(crawler)
        results.append(result)

    end_time = datetime.now()
    elapsed = end_time - start_time

    # -----------------------------------------------------
    # 최종 결과 요약
    # -----------------------------------------------------
    print()
    print("=" * 72, flush=True)
    print(" ALL CRAWLERS - FINAL SUMMARY", flush=True)
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

    # 하나라도 실패하면 GitHub Actions를 실패 상태로 표시.
    # 단, 모든 크롤러는 끝까지 실행한 뒤에 최종 실패 처리합니다.
    if fail_count > 0:
        print("[RESULT] One or more crawlers failed, timed out, or were skipped.", flush=True)
        sys.exit(1)

    print("[RESULT] All crawlers completed successfully.", flush=True)
    sys.exit(0)

if __name__ == "__main__":
    main()
