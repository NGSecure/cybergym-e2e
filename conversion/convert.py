#!/usr/bin/env python3
"""Convert CyberGym-E2E (sunblaze-ucb/cybergym-e2e) tasks into new-platform Harbor bundles.

Every upstream task ("original") becomes one bundle per upstream mode. The two bundles of a
task share one base image (upstream build image + setup_workspace + prepare.sh) and one
task-server (judge) image:

    poc_bundles/<project>-<task>-e2e/       e2e mode: find the bug, deliver PoC + patch
    patch_bundles/<project>-<task>-patch/   patch-only mode: given crash.log + PoC, deliver a patch

Inputs are the upstream task definitions in original/ (a checkout of the upstream GitHub repo
at the pinned commit, or a copy of it carrying an UPSTREAM_COMMIT file) and the HF dataset's
file inventory (conversion/hf_inventory.json). The gated data (src.tgz, poc.bin, crash.log) is
not needed to convert; each bundle's build.sh uses original/projects/... when present and
downloads it there otherwise.

    python3 conversion/convert.py                                  # every task of scripts/tasks.txt
                                                                   # present in original/projects, both modes
    python3 conversion/convert.py --task readstat/arvo_33991       # one task
"""

import argparse
import ast
import base64
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import tomllib                      # Python 3.11+
except ImportError:                     # Python 3.8-3.10: the same parser, packaged as tomli
    try:
        import tomli as tomllib
    except ImportError:
        sys.exit("convert.py needs Python 3.11+, or tomli on older Pythons (pip install tomli)")

try:
    import tomli_w
except ImportError:  # upstream sanitizes config.toml with tomli_w; reproduce it byte for byte
    sys.exit("convert.py needs tomli_w (pip install tomli-w), the writer upstream uses for config.toml")

CONV = Path(__file__).resolve().parent
TEMPLATES = CONV / "templates"
UPSTREAM = json.loads((CONV / "upstream.json").read_text())
INVENTORY = json.loads((CONV / "hf_inventory.json").read_text())

MODES = {
    # mode: (output dir, bundle suffix, main-image suffix)
    "e2e": ("poc_bundles", "e2e", "e2e"),
    "patch-only": ("patch_bundles", "patch", "patch"),
}
BUILD_SCRIPTS = ("prepare.sh", "compile.sh", "run_poc.sh", "test.sh")
# Network fetches in the scripts that run after prepare.sh, i.e. when the judge validates.
# Upstream keeps most of them offline-capable (apt --no-download, pip --no-index from files
# prepare.sh pre-downloads) but validates with network access, so a few are not. A heuristic
# for triage: a hit behind an offline fallback ("cp /deps/... || wget ...") is listed too.
ONLINE_FETCH = (
    ("apt", re.compile(r"\bapt(-get)?\s+(-\S+\s+)*(install|update)\b"), "--no-download"),
    ("pip", re.compile(r"\bpip3?\s+(-\S+\s+)*install\b"), "--no-index"),
    ("git", re.compile(r"\bgit\s+(clone|fetch|pull|submodule\s+update)\b"), None),
    ("download", re.compile(r"\b(wget|curl)\b.*\b(https?|ftp)://"), None),
)


def online_fetches(script_text):
    found = []
    for line in script_text.replace("\\\n", " ").splitlines():
        line = line.strip()
        if line.startswith("#"):
            continue
        for label, pattern, offline_flag in ONLINE_FETCH:
            if label not in found and pattern.search(line) and not (offline_flag and offline_flag in line):
                found.append(label)
    return found
# Fixed per-repository salt: tokens stay stable across re-runs, so images need no rebuild.
TOKEN_SALT = b"cge2e/9f4c1d7e2b"

# Harbor installs the agent CLI inside the sandbox when a trial starts. For the Node-based
# agents that goes through nvm, whose installer clones nvm from GitHub with git. Some upstream
# prepare.sh scripts point git at an offline mirror for whole URL prefixes
# (git config --global url.<mirror>.insteadOf <prefix>); a prefix that covers the nvm
# repository makes that clone fail. Such a sandbox gets a one-prefix exemption
# (templates/nvm_exempt_block.txt) and its image a tag of its own, so the corrected image never
# hides behind an address that a registry or a sandbox node has already cached.
NVM_PREFIX = "https://github.com/nvm-sh/"
NVM_EXEMPT_TAG_SUFFIX = "-r1"
GIT_REWRITE = re.compile(r"""\bgit\s+config\s+--global\s+(?:--\S+\s+)*url\.\S+\.insteadOf\s+["']?([^"'\s]+)""")


def rewritten_prefixes(script_text):
    """URL prefixes a script redirects with a global git insteadOf rule."""
    return [m.group(1) for line in script_text.splitlines() if not line.lstrip().startswith("#")
            for m in GIT_REWRITE.finditer(line)]


def render(template, values):
    text = (TEMPLATES / template).read_text()
    for key, value in values.items():
        text = text.replace("@@%s@@" % key, str(value))
    leftover = sorted(set(re.findall(r"@@[A-Z0-9_]+@@", text)))
    if leftover:
        raise SystemExit("%s: unfilled placeholders %s" % (template, leftover))
    return text


def write(path, content, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode() if isinstance(content, str) else content
    path.write_bytes(data)
    os.chmod(path, mode)


def toml_str(value):
    return json.dumps(value, ensure_ascii=False)


def upstream_commit(repo):
    """Commit of the upstream task definitions: git HEAD, or the UPSTREAM_COMMIT file of a copy."""
    if (repo / ".git").exists():
        return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()
    marker = repo / "UPSTREAM_COMMIT"
    return marker.read_text().strip() if marker.is_file() else "unknown (no .git, no UPSTREAM_COMMIT)"


def load_prompt(upstream):
    """upstream scripts/run_agent.py get_prompt_iterative(), the default prompt style."""
    path = upstream / "scripts" / "run_agent.py"
    tree = ast.parse(path.read_text(), str(path))
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "get_prompt_iterative")
    namespace = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["get_prompt_iterative"]


def check_vendor(upstream):
    for name in ("validate.py", "install_validate_deps.sh"):
        ours = (CONV / "vendor" / name).read_bytes()
        theirs = (upstream / "scripts" / name).read_bytes()
        if ours != theirs:
            raise SystemExit("conversion/vendor/%s differs from upstream scripts/%s" % (name, name))


class Task:
    def __init__(self, upstream, name):
        self.name = name                                    # e.g. leptonica/arvo_31179
        self.project, self.taskdir = name.split("/")
        self.dir = upstream / "projects" / self.project / self.taskdir
        cfg = tomllib.loads((self.dir.parent / "project.toml").read_text())
        cfg.update(tomllib.loads((self.dir / "config.toml").read_text()))   # as run_agent.py main()
        if cfg.get("pre_patches") or cfg.get("pre_patch"):
            raise SystemExit("%s: pre_patches are not supported by this converter" % name)
        self.cfg = cfg
        self.repo_to_patch = cfg.get("repo_to_patch", "")
        if not re.fullmatch(r"[A-Za-z0-9._+-]+", self.repo_to_patch):
            raise SystemExit("%s: unexpected repo_to_patch %r" % (name, self.repo_to_patch))
        self.build_image = cfg.get("build_image", UPSTREAM["default_build_image"])
        self.source = "arvo" if self.taskdir.startswith("arvo_") else "oss-fuzz"
        self.slug = "%s-%s" % (self.project, self.taskdir.replace("_", "-").lower())
        self.key = hashlib.sha256(name.encode()).hexdigest()[:10]
        self.token = hashlib.sha256(b"cge2e-auth\0" + name.encode() + b"\0" + TOKEN_SALT).hexdigest()[:32]
        compile_sh = (self.dir / "compile.sh").read_text()
        found = re.findall(r"^\s*export SANITIZER=(\S+)", compile_sh, re.M)
        self.sanitizer = "+".join(sorted(set(found))) or "unknown"   # one task picks per tree state
        files = INVENTORY["files"]
        hf = "projects/%s" % name
        self.sizes = {f: files["%s/%s" % (hf, f)] for f in ("src.tgz", "poc.bin", "crash.log")
                      if "%s/%s" % (hf, f) in files}
        for need in ("src.tgz", "poc.bin"):
            if need not in self.sizes:
                raise SystemExit("%s: %s missing from the HF inventory" % (name, need))
        self.patch = (self.dir / "patch.diff").read_bytes()
        self.notes = []
        if "crash.log" not in self.sizes:
            self.notes.append("no crash.log upstream (patch-only agents get poc.bin only)")
        if self.sizes["poc.bin"] == 0:
            self.notes.append("ground-truth poc.bin is empty upstream")
        online = []
        for script in ("compile.sh", "run_poc.sh", "test.sh"):
            hits = online_fetches((self.dir / script).read_text(errors="replace"))
            if hits:
                online.append("%s: %s" % (script, "+".join(hits)))
        self.online = ", ".join(online)
        if online:
            self.notes.append("judge-time network fetch (%s), upstream validates online" % self.online)
        prepare = (self.dir / "prepare.sh").read_text(errors="replace")
        self.nvm_exempt = any(NVM_PREFIX.startswith(p) for p in rewritten_prefixes(prepare))
        if self.nvm_exempt:
            self.notes.append("sandbox git config keeps %s off prepare.sh's offline GitHub mirror, "
                              "so Harbor can install the agent CLI" % NVM_PREFIX)

    def base_files(self):
        """Build-context files of the shared base image: name -> (bytes, mode)."""
        files = {
            "validate.py": ((CONV / "vendor" / "validate.py").read_bytes(), 0o644),
            "install_validate_deps.sh": ((CONV / "vendor" / "install_validate_deps.sh").read_bytes(), 0o644),
        }
        for script in BUILD_SCRIPTS:
            path = self.dir / script
            files[script] = (path.read_bytes(), 0o755 if os.stat(path).st_mode & 0o111 else 0o644)
        sanitized = {"repo_to_patch": self.repo_to_patch,
                     "immutable_files": self.cfg.get("immutable_files", [])}   # utils.py setup_workspace
        files["config.toml"] = (tomli_w.dumps(sanitized).encode(), 0o644)
        return files


def convert_task(task, modes, out, args, prompt):
    ver = args.version_tag
    base_tag = "cge2e_%s_base:%s" % (task.key, ver)
    ts_tag = "cge2e_%s_ts:%s" % (task.key, ver)
    cleanup = "arvo" in task.build_image or "cybergym" in task.build_image   # utils.py:190
    base_dockerfile = render("base.Dockerfile", {
        "UPSTREAM_TASK": task.name,
        "UPSTREAM_COMMIT_SHORT": UPSTREAM["github"]["commit"][:7],
        "BUILD_IMAGE": task.build_image,
        "CLEANUP_BLOCK": (TEMPLATES / "cleanup_block.txt").read_text() if cleanup else "",
        "REPO_TO_PATCH": task.repo_to_patch,
    })
    base_files = task.base_files()
    digest = hashlib.sha256(b"cge2e-base-v1\0" + base_dockerfile.encode())
    for name in sorted(base_files):
        content, mode = base_files[name]
        digest.update(("\0%s\0%o\0" % (name, mode)).encode() + hashlib.sha256(content).digest())
    digest.update(("\0%s\0%s\0%d" % (INVENTORY["revision"], task.name, task.sizes["src.tgz"])).encode())
    base_digest = digest.hexdigest()[:16]

    rows = []
    for mode in modes:
        dataset, suffix, main_suffix = MODES[mode]
        bundle_name = "%s-%s" % (task.slug, suffix)
        bdir = out / dataset / bundle_name
        main_tag = "cge2e_%s_main_%s:%s%s" % (task.key, main_suffix, ver,
                                              NVM_EXEMPT_TAG_SUFFIX if task.nvm_exempt else "")
        values = {
            "BUNDLE": bundle_name,
            "BUNDLE_DIR": "%s/%s" % (dataset, bundle_name),
            "UPSTREAM_TASK": task.name,
            "UPSTREAM_COMMIT_SHORT": UPSTREAM["github"]["commit"][:7],
            "MODE": mode,
            "BASE_TAG": base_tag,
            "MAIN_TAG": main_tag,
            "TS_TAG": ts_tag,
            "AUTH_TOKEN": task.token,
        }

        refs = load_refs(bdir, main_tag, ts_tag)
        for sub in ("environment", "tests", "solution"):
            shutil.rmtree(bdir / sub, ignore_errors=True)

        env = bdir / "environment"
        if mode == "e2e":
            mode_block = (TEMPLATES / "mode_block_e2e.txt").read_text()
        else:
            has_log = "crash.log" in task.sizes
            mode_block = render("mode_block_patch.txt", {
                "PATCH_FILES": "data/crash.log data/poc.bin" if has_log else "data/poc.bin",
                "PATCH_FILES_DESC": "crash.log and poc.bin" if has_log
                else "poc.bin (this task has no crash.log upstream)",
            })
        if task.nvm_exempt:
            mode_block += (TEMPLATES / "nvm_exempt_block.txt").read_text()
        write(env / "Dockerfile", render("main.Dockerfile", dict(values, MODE_BLOCK=mode_block)))
        write(env / ".dockerignore", "base/\ntask-server/\n")
        write(env / "docker-compose.yaml", render("docker-compose.yaml", dict(
            values, TS_CPUS=args.ts_cpus, TS_MEMORY_MB=args.ts_memory_mb)))
        write(env / "base" / "Dockerfile", base_dockerfile)
        for name, (content, fmode) in base_files.items():
            write(env / "base" / "files" / name, content, fmode)
        write(env / "task-server" / "Dockerfile", render("task-server.Dockerfile", values))
        write(env / "task-server" / "task_server.py", (TEMPLATES / "task_server.py").read_text(), 0o755)

        write(bdir / "tests" / "test.sh", render("test.sh", values), 0o755)
        b64 = base64.encodebytes(task.patch).decode().rstrip("\n")
        poc_block = render("solve_poc_block.txt", values) if mode == "e2e" else ""
        write(bdir / "solution" / "solve.sh", render("solve.sh", dict(
            values, PATCH_B64=b64, SOLVE_POC_BLOCK=poc_block,
            SOLVE_POC_NOTE=(" and the ground-truth PoC to /output/poc.bin." if mode == "e2e" else "."))), 0o755)

        instruction = prompt("e2e" if mode == "e2e" else "patch-only", task.repo_to_patch, "")
        write(bdir / "instruction.md", instruction.rstrip("\n") + "\n")

        sizes = " ".join("%s=%d" % kv for kv in sorted(task.sizes.items()))
        write(bdir / "build.sh", render("build.sh", dict(
            values, HF_REPO=UPSTREAM["huggingface"]["repo"], HF_REV=UPSTREAM["huggingface"]["revision"],
            REPO_TO_PATCH=task.repo_to_patch, BASE_DIGEST=base_digest, HF_SIZES=sizes)), 0o755)
        write(bdir / "push.sh", render("push.sh", values), 0o755)

        source_label = "ARVO" if task.source == "arvo" else "OSS-Fuzz"
        notes = [n for n in task.notes if mode == "patch-only" or "crash.log" not in n]
        what = ("find the vulnerability, write a PoC that crashes the sanitizer build and a patch that "
                "fixes it without breaking the project's tests" if mode == "e2e" else
                "patch the vulnerability shown by a sanitizer crash log and its PoC without breaking "
                "the project's tests")
        task_toml = render("task.toml", dict(
            values,
            TASK_NAME="cybergym-e2e-%s" % bundle_name,
            DESCRIPTION=toml_str("CyberGym-E2E %s task (%s, %s): %s. Graded by the upstream validate.py "
                                 "stages in a separate judge container." % (mode, task.project, source_label, what)),
            KEYWORDS=", ".join(toml_str(k) for k in (
                "cybergym-e2e", mode, task.source, task.project, *task.sanitizer.split("+"), "vulnerability",
                "patch") + (("poc",) if mode == "e2e" else ())),
            UPSTREAM_TASK_ID=task.cfg.get("task_id", ""),
            SOURCE=task.source,
            PROJECT=task.project,
            LANGUAGE=task.cfg.get("language", ""),
            SANITIZER=task.sanitizer,
            FUZZ_TARGET=task.cfg.get("target_prog", ""),
            DIFFICULTY="hard" if mode == "e2e" else "medium",
            DATA_NOTES=", ".join(toml_str(n) for n in notes),
            UPSTREAM_COMMIT=UPSTREAM["github"]["commit"],
            HF_REV=UPSTREAM["huggingface"]["revision"],
            VERSION_TAG=ver,
            VERIFIER_TIMEOUT="%.1f" % (args.verifier_timeout_e2e if mode == "e2e" else args.verifier_timeout_patch),
            AGENT_TIMEOUT="%.1f" % args.agent_timeout,
            BUILD_TIMEOUT="%.1f" % args.build_timeout,
            CPUS=args.cpus,
            MEMORY_MB=args.memory_mb,
        ))
        for role, remote in refs.items():   # keep SWR addresses a previous push.sh backfilled
            task_toml = re.sub(r'(?m)^%s = ".*"$' % role, '%s = "%s"' % (role, remote), task_toml)
        tomllib.loads(task_toml)            # must stay valid TOML
        write(bdir / "task.toml", task_toml)

        rows.append({
            "bundle": "%s/%s" % (dataset, bundle_name), "mode": mode, "upstream_task": task.name,
            "upstream_task_id": task.cfg.get("task_id", ""), "source": task.source,
            "project": task.project, "sanitizer": task.sanitizer,
            "fuzz_target": task.cfg.get("target_prog", ""), "build_image": task.build_image,
            "image_cleanup": int(cleanup), "repo_to_patch": task.repo_to_patch,
            "src_tgz_bytes": task.sizes["src.tgz"], "poc_bytes": task.sizes["poc.bin"],
            "crash_log_bytes": task.sizes.get("crash.log", ""), "patch_bytes": len(task.patch),
            "base_image": base_tag, "main_image": main_tag, "task_server_image": ts_tag,
            "base_digest": base_digest, "judge_online_fetch": task.online, "notes": "; ".join(notes),
        })
    return rows


def load_refs(bdir, main_tag, ts_tag):
    """SWR addresses from an earlier push.sh, if they belong to the same local tags."""
    path = bdir / "image_refs.json"
    if not path.is_file():
        return {}
    try:
        images = json.loads(path.read_text()).get("images", {})
    except ValueError:
        return {}
    want = {"docker_image": main_tag, "task_server_image": ts_tag}
    refs = {}
    for role, local in want.items():
        rec = images.get(role) or {}
        remote = rec.get("remote") or rec.get("remote_image")
        if remote and (rec.get("local") or rec.get("local_image")) == local:
            refs[role] = remote
    return refs


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--upstream", type=Path, default=CONV.parent / "original",
                    help="upstream task definitions (default: %(default)s)")
    ap.add_argument("--task", action="append",
                    help="project/task_dir to convert (default: the scripts/tasks.txt tasks present in --upstream)")
    ap.add_argument("--mode", action="append", choices=sorted(MODES), help="default: both modes")
    ap.add_argument("--out", type=Path, default=CONV.parent, help="repository root (default: %(default)s)")
    ap.add_argument("--version-tag", default="v0.0.1")
    ap.add_argument("--cpus", type=int, default=4, help="agent sandbox CPUs")
    ap.add_argument("--memory-mb", type=int, default=16384, help="agent sandbox memory")
    ap.add_argument("--ts-cpus", type=int, default=4, help="judge CPUs (compose limit)")
    ap.add_argument("--ts-memory-mb", type=int, default=16384, help="judge memory (compose limit)")
    ap.add_argument("--agent-timeout", type=float, default=float(UPSTREAM["agent_timeout_sec"]))
    ap.add_argument("--verifier-timeout-e2e", type=float, default=10800.0)
    ap.add_argument("--verifier-timeout-patch", type=float, default=7200.0)
    ap.add_argument("--build-timeout", type=float, default=7200.0)
    ap.add_argument("--report", type=Path, default=CONV / "conversion_report.csv")
    args = ap.parse_args()

    upstream = args.upstream.resolve()
    head = upstream_commit(upstream)
    if head != UPSTREAM["github"]["commit"]:
        raise SystemExit("%s is at %s; check out the pinned commit %s" % (upstream, head, UPSTREAM["github"]["commit"]))
    if INVENTORY["revision"] != UPSTREAM["huggingface"]["revision"]:
        raise SystemExit("hf_inventory.json is for another revision than upstream.json pins")
    check_vendor(upstream)
    prompt = load_prompt(upstream)

    if args.task:
        names = args.task
    else:   # every listed task whose definition is in this copy (original/ may hold a subset)
        listed = [l.strip() for l in (upstream / "scripts" / "tasks.txt").read_text().splitlines() if l.strip()]
        names = [n for n in listed if (upstream / "projects" / n / "config.toml").is_file()]
        print("scripts/tasks.txt lists %d tasks, %d of them present under %s/projects"
              % (len(listed), len(names), upstream))
        if not names:
            raise SystemExit("nothing to convert")
    modes = args.mode or ["e2e", "patch-only"]
    tasks = [Task(upstream, n) for n in names]
    keys = {}
    for t in tasks:
        if keys.setdefault(t.key, t.name) != t.name:
            raise SystemExit("image key collision: %s and %s" % (keys[t.key], t.name))

    rows = []
    for t in tasks:
        rows += convert_task(t, modes, args.out.resolve(), args, prompt)

    if args.task is None and args.mode is None:
        with open(args.report, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        print("report: %s" % args.report)
    print("converted %d task(s) into %d bundle(s)" % (len(tasks), len(rows)))


if __name__ == "__main__":
    main()
