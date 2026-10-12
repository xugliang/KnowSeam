"""Run database E2E in a new disposable container, never the production database.

Requires an already-present PostgreSQL 17 + pgvector image. No image pull,
host volume, system change, broad cleanup or existing-container operation.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import uuid


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, stderr=subprocess.DEVNULL).strip()


def checked_targets(root, values):
    test_root = (root / "tests").resolve()
    if not values:
        raise ValueError("at least one project test target required")
    for value in values:
        candidate = (root / value).resolve()
        if (value.startswith("-") or ".." in Path(value).parts
                or not candidate.is_relative_to(test_root) or not candidate.exists()):
            raise ValueError("only existing project test targets are accepted")
    return list(values)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True, help="Existing PostgreSQL/pgvector image; never pulled")
    parser.add_argument("--tests", nargs="+", default=["tests/test_postgres_e2e.py"])
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    targets = checked_targets(root, args.tests)
    name = "knowseam-test-" + uuid.uuid4().hex[:12]
    password = secrets.token_urlsafe(32)
    identifier = None
    with tempfile.TemporaryDirectory(prefix="knowseam-db-test-") as directory:
        env_file = Path(directory) / "postgres.env"
        env_file.write_text("POSTGRES_USER=knowseam\nPOSTGRES_DB=knowseam_test\nPOSTGRES_PASSWORD=" + password + "\n")
        env_file.chmod(0o600)
        try:
            benchmark_options = (['-c','shared_buffers=32MB','-c','max_wal_size=64MB','-c','min_wal_size=32MB',
                                  '-c','max_parallel_workers_per_gather=0']
                                 if os.environ.get('KNOWSEAM_BENCHMARK_SIZE') else [])
            identifier = docker("run", "--rm", "--detach", "--pull=never", "--name", name,
                                "--label", "knowseam.test=" + name, "--memory=512m", "--cpus=1", "--pids-limit=128",
                                "--tmpfs", "/var/lib/postgresql/data:rw,size=512m",
                                "--publish", "127.0.0.1::5432", "--env-file", str(env_file), args.image, *benchmark_options)
            port = json.loads(docker("inspect", identifier))[0]["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"]
            for _ in range(60):
                check = subprocess.run(["docker", "exec", identifier, "pg_isready", "-U", "knowseam",
                                        "-d", "knowseam_test"], capture_output=True)
                if check.returncode == 0:
                    break
                time.sleep(.5)
            else:
                raise RuntimeError("test database did not become ready")
            env = dict(os.environ)
            env["SUITE_KNOWLEDGE_TEST_DSN"] = f"postgresql://knowseam:{password}@127.0.0.1:{port}/knowseam_test"
            result = subprocess.run([sys.executable, "-m", "pytest", *targets, "-q",
                                     "--tb=short", "--basetemp", str(Path(directory) / "pytest")], cwd=root, env=env)
            if result.returncode:
                state = json.loads(docker('inspect',identifier))[0]['State']
                print(json.dumps({'isolated_db_failure':{'oom_killed':state.get('OOMKilled'),
                                 'exit_code':state.get('ExitCode')}}))
            elif benchmark_options:
                report_path = root / '.build-three-phases' / 'benchmarks' / ('synthetic-'+os.environ['KNOWSEAM_BENCHMARK_SIZE']+'.json')
                if report_path.is_file():
                    report = json.loads(report_path.read_text())
                    try:
                        peak = int(docker('exec',identifier,'cat','/sys/fs/cgroup/memory.peak'))
                    except Exception:
                        peak = None
                    report['isolated_container'] = {'memory_limit_bytes':512*1024*1024,'memory_peak_bytes':peak,
                        'cpu_limit':1,'shared_buffers':'32MB','max_wal_size':'64MB','parallel_workers_per_gather':0}
                    report_path.write_text(json.dumps(report,indent=2)+'\n')
            return result.returncode
        finally:
            if identifier:
                info = json.loads(docker("inspect", identifier))[0]
                if info["Id"] == identifier and info["Config"]["Labels"].get("knowseam.test") == name:
                    docker("stop", "--time=5", identifier)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("Isolated database verification failed; production containers were not targeted.", file=sys.stderr)
        raise SystemExit(1) from None
