import os
import subprocess
import sys

def run_all_crawlers():
    # 1. 실행할 대상 폴더 리스트 (이미지 기반 확인된 폴더들)
    # 특정 폴더만 타겟팅하거나, 루트의 모든 폴더를 대상으로 할 수 있습니다.
    target_folders = [
        "Alphaliner",
        "BunkerPrice",
        "Forwarder",
        "GDP&Inflation",
        "Shipping Index",
        "TAC Index"
    ]
    
    # 실행 결과 기록
    results = []

    # 폴더를 알파벳 순으로 정렬하여 실행 순서 보장
    target_folders.sort()

    print(f"🚀 Total {len(target_folders)} modules identified. Starting automation...\n")

    for folder in target_folders:
        # 해당 폴더 내의 .py 파일 찾기 (예: folder/alphaliner.py)
        if not os.path.exists(folder):
            print(f"⚠️ Skip: Folder '{folder}' not found.")
            continue

        # 폴더 내의 모든 .py 파일을 탐색
        py_files = [f for f in os.listdir(folder) if f.endswith('.py') and f != 'run_all.py']
        
        if not py_files:
            print(f"⚠️ Skip: No .py files found in '{folder}'.")
            continue

        for py_file in py_files:
            file_path = os.path.join(folder, py_file)
            print(f"📦 Executing: {file_path}")
            
            try:
                # subprocess를 사용하여 각 스크립트를 독립된 프로세스로 실행
                # cwd를 설정하여 스크립트가 자기 폴더 내의 db를 찾을 수 있도록 함
                result = subprocess.run(
                    [sys.executable, py_file], 
                    cwd=folder, 
                    check=True, 
                    capture_output=True, 
                    text=True
                )
                print(f"✅ Success: {py_file}")
                results.append((folder, py_file, "Success"))
            except subprocess.CalledProcessError as e:
                print(f"❌ Failed: {py_file}")
                print(f"Error Detail: {e.stderr}")
                results.append((folder, py_file, f"Failed: {e.stderr[:50]}..."))
            except Exception as e:
                print(f"❌ Unexpected Error: {e}")
                results.append((folder, py_file, f"Error: {str(e)}"))

    print("\n" + "="*30)
    print("📊 Final Execution Summary")
    print("="*30)
    for res in results:
        print(f"[{res[0]}] {res[1]} -> {res[2]}")
    
    # 모든 크롤링이 끝난 후 대시보드 생성기 호출
    print("\n🛠️ Generating Dashboard...")
    try:
        subprocess.run([sys.executable, "dashboard_generator.py"], check=True)
        print("🎉 All processes completed successfully!")
    except Exception as e:
        print(f"⚠️ Crawler finished, but Dashboard generation failed: {e}")

if __name__ == "__main__":
    run_all_crawlers()
