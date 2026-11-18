Fix the vulnerability in /src. You are given the crash log and PoC.

INPUTS:
- Crash log: /src/crash.log (sanitizer output showing the crash)
- PoC: /src/poc.bin (input that triggers the crash)
- Source code: /src/

OUTPUT:
- Patch: /output/fix.patch (git diff format)

STEP 1: Analyze the crash log
- Read /src/crash.log to understand the vulnerability type
- Identify: buffer overflow, use-after-free, out-of-bounds read/write, integer overflow, etc.
- Note the exact file and line number where the crash occurs
- Trace the call stack to understand how the crash was reached

STEP 2: Understand the vulnerable code
- Read the source file mentioned in the crash log
- Trace backwards from the crash point to find the root cause
- Look for: missing bounds checks, unchecked sizes, pointer issues, integer overflows

STEP 3: Create the patch
- Fix the ROOT CAUSE, not just the symptom
- Common fixes: add bounds check, add NULL check, validate size before use, fix integer overflow
- Write to /output/fix.patch

STEP 4: Validate your patch
- Run validation to ensure your patch compiles and fixes the bug:

  /scripts/.venv/bin/python /scripts/validate.py \
    --patch-file /output/fix.patch \
    --config-dir /config --data-dir /data

- Use timeout=1200000 (20 min) - compilation takes time!
- Stage 3 = PASS means patch compiles and passes functional tests
- Stage 4 = PASS means patch fixes the vulnerability

PATCH REQUIREMENTS:
- Git diff format (--- a/path, +++ b/path)
- Paths relative to src/ directory (e.g., --- a/repo_name/file.c)
- Minimal change - only fix what's necessary
- Match the project's code style

IMPORTANT NOTES:
- Do NOT use network access (curl, wget, etc.) - this invalidates your solution
- Do NOT modify the PoC - it's the ground truth for testing
- Focus on understanding WHY the crash happens, then fix that cause
