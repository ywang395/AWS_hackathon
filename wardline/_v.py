import subprocess, sys, pathlib
w = pathlib.Path(r"c:\Users\alish\OneDrive\Documents\GitHub\AWS_hackathon\wardline")
out = []
def run(label, args, **env):
    import os
    e = dict(os.environ); e.update(env)
    r = subprocess.run([sys.executable, *args], capture_output=True, text=True, cwd=w, env=e)
    tail = "\n".join(r.stdout.strip().splitlines()[-4:])
    out.append(f"===== {label} (rc={r.returncode}) =====\n{tail}\n{r.stderr.strip()[-300:]}")

# Box: full suite (covers /chat brief+reply+patients+deliveries, request_med, demo cases, DynamoDB path, email)
run("FULL SUITE", ["-m","pytest","-q","-p","no:cacheprovider"])
# Box 3: DynamoDB go-live against the fake resource (DATA_BACKEND set by the script)
run("DYNAMODB go_live (fake via test)", ["-m","pytest","-q","-p","no:cacheprovider",
    "backend/tests/test_go_live_scripts.py","backend/tests/test_dynamodb_backend.py"])
# Box 3: seed dry-run CLI actually runs
run("SEED --dry-run CLI", ["scripts/seed.py","--dry-run"])
# Box 4: email go-live in DRY RUN (no AWS) still exercises the path end to end
run("EMAIL go_live tests", ["-m","pytest","-q","-p","no:cacheprovider","backend/tests/test_go_live_scripts.py","-k","email"])
import shutil
for p in list(w.rglob("__pycache__"))+[w/".pytest_tmp",w/".pytest_cache"]:
    shutil.rmtree(p, ignore_errors=True)
(w.parent/"_v.txt").write_text("\n\n".join(out), encoding="utf-8")