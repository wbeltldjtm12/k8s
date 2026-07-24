import subprocess
import sys
import time
import os
import threading


def load_env_values(path):
    """간단한 KEY=VALUE 형식의 env 파일을 읽습니다."""
    values = {}
    with open(path, "r", encoding="utf-8") as stream:
        for raw_line in stream:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if not key:
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            values[key] = value
    return values


def log_reader(pipe, prefix):
    try:
        for line in iter(pipe.readline, ''):
            if line:
                print(f"[{prefix}] {line.strip()}")
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass

def main():
    # Detect the KubeIn root directory
    base_dir = os.path.dirname(os.path.abspath(__file__))
    backend_dir = os.path.join(base_dir, "backend")
    frontend_dir = os.path.join(base_dir, "frontend")

    print("=" * 60)
    print(" KubeIn 통합 실행기 (FastAPI Backend + Streamlit Frontend)")
    print("=" * 60)

    # 1. 설정 파일을 복사하지 않고 자식 프로세스 환경으로만 로드
    cluster_env_path = os.path.join(base_dir, "cluster-port", ".env.cluster")
    backend_env_path = os.path.join(backend_dir, ".env")

    file_env = {}
    loaded_env_files = []
    for env_path in (cluster_env_path, backend_env_path):
        if not os.path.exists(env_path):
            continue
        try:
            file_env.update(load_env_values(env_path))
            loaded_env_files.append(os.path.relpath(env_path, base_dir))
        except Exception as e:
            print(f"[경고] 설정 파일 로드 실패 ({env_path}): {e}")

    backend_env = file_env.copy()
    frontend_env = os.environ.copy()
    backend_env.update(os.environ)  # 명시적으로 설정한 OS 환경변수가 파일보다 우선
    if "BACKEND_URL" in file_env and "BACKEND_URL" not in frontend_env:
        frontend_env["BACKEND_URL"] = file_env["BACKEND_URL"]
    if loaded_env_files:
        print(f"[*] 환경 설정 로드: {', '.join(loaded_env_files)}")

    # 2. 백엔드 및 프론트엔드 커맨드 설정
    # sys.executable을 사용하여 현재 실행 중인 파이썬 환경(가상환경 등)을 그대로 계승합니다.
    backend_cmd = [sys.executable, "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
    frontend_cmd = [sys.executable, "-m", "streamlit", "run", "app.py", "--server.port", "8501", "--server.address", "0.0.0.0"]

    print(f"[*] 백엔드 실행 중... (포트 8000)")
    backend_proc = subprocess.Popen(
        backend_cmd,
        cwd=backend_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=backend_env,
    )

    print(f"[*] 프론트엔드 실행 중... (포트 8501)")
    try:
        frontend_proc = subprocess.Popen(
            frontend_cmd,
            cwd=frontend_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=frontend_env,
        )
    except Exception:
        backend_proc.terminate()
        try:
            backend_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            backend_proc.kill()
            backend_proc.wait()
        raise

    # 3. 비동기 로그 출력 스레드 가동
    t1 = threading.Thread(target=log_reader, args=(backend_proc.stdout, "Backend"), daemon=True)
    t2 = threading.Thread(target=log_reader, args=(frontend_proc.stdout, "Frontend"), daemon=True)
    t1.start()
    t2.start()

    print("[*] 두 서비스가 모두 실행되었습니다. 중지하려면 Ctrl+C를 누르세요.")
    print("-" * 60)

    try:
        while True:
            # 두 프로세스 중 하나라도 죽었는지 감시
            if backend_proc.poll() is not None:
                print(f"\n[!] 백엔드 프로세스가 종료되었습니다 (종료 코드: {backend_proc.returncode}).")
                break
            if frontend_proc.poll() is not None:
                print(f"\n[!] 프론트엔드 프로세스가 종료되었습니다 (종료 코드: {frontend_proc.returncode}).")
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[*] 종료 신호 감지. 프로세스를 정리하는 중...")
    finally:
        # 종료 처리
        backend_proc.terminate()
        frontend_proc.terminate()
        
        try:
            backend_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            backend_proc.kill()
            
        try:
            frontend_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            frontend_proc.kill()
            
        print("[*] 모든 서비스가 안전하게 종료되었습니다.")

if __name__ == "__main__":
    main()
