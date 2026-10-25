#!/usr/bin/env python3
"""
ShadowRunner CI Fallback Router
Selects between Shadow PC self-hosted runner and GitHub-hosted runners.
Modes:
  - auto: Prefers online/idle Shadow runner; waits up to timeout if busy; falls back to GitHub-hosted if offline/timeout.
  - shadow: Forces Shadow PC self-hosted runner.
  - github: Forces GitHub-hosted runner.
"""

import os
import sys
import json
import time
import argparse
import urllib.request
import urllib.error

def query_org_runners(org, token):
    url = f"https://api.github.com/orgs/{org}/actions/runners"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ShadowRunner-Router"
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[Router Warning] Failed to query org runners: {e}", file=sys.stderr)
        return None

def set_github_output(key, value):
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")
    print(f"[Output] {key} = {value}")

def set_workflow_summary(markdown_text):
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(markdown_text + "\n")

def main():
    parser = argparse.ArgumentParser(description="ShadowRunner CI Fallback Router")
    parser.add_argument("--mode", default="auto", choices=["auto", "shadow", "github"], help="Routing mode")
    parser.add_argument("--org", default="jayj3nks-dev", help="GitHub Organization")
    parser.add_argument("--token", default=os.environ.get("ROUTER_PAT", ""), help="GitHub PAT with org read access")
    parser.add_argument("--required-os", default="linux", choices=["linux", "windows"], help="Required OS for workload")
    parser.add_argument("--needs-gpu", action="store_true", help="Flag if workload requires GPU")
    parser.add_argument("--wait-timeout", type=int, default=300, help="Max wait seconds if Shadow runner is busy")
    parser.add_argument("--poll-interval", type=int, default=15, help="Poll interval in seconds")
    parser.add_argument("--is-fork", action="store_true", help="True if PR is from an untrusted fork")
    parser.add_argument("--runner-name", default="shadow-org-runner", help="Name of Shadow organization runner")
    args = parser.parse_args()

    print("==================================================")
    print(" SHADOWRUNNER CI INTELLIGENT ROUTER")
    print("==================================================")
    print(f"Organization:       {args.org}")
    print(f"Requested Mode:     {args.mode}")
    print(f"Required OS:        {args.required_os}")
    print(f"Requires GPU:       {args.needs_gpu}")
    print(f"Is Untrusted Fork:  {args.is_fork}")
    print(f"Busy Wait Timeout:  {args.wait_timeout}s")
    print("--------------------------------------------------")

    # 1. Untrusted Fork PR Protection
    if args.is_fork:
        print("[Router Security] Untrusted fork PR detected. Enforcing ephemeral GitHub-hosted runner.")
        hosted_runner = "windows-latest" if args.required_os == "windows" else "ubuntu-latest"
        reason = "Untrusted fork PR: isolated to ephemeral GitHub-hosted runner for host protection."
        set_github_output("target", "github-hosted")
        set_github_output("runner", json.dumps(hosted_runner))
        set_github_output("reason", reason)
        set_github_output("mode", args.mode)
        set_github_output("waited_seconds", "0")
        set_workflow_summary(f"""### 🛡️ ShadowRunner Routing Decision
| Parameter | Value |
| :--- | :--- |
| **Selected Target** | `GitHub-hosted` (`{hosted_runner}`) |
| **Requested Mode** | `{args.mode}` |
| **Routing Reason** | {reason} |
| **Security Status** | Sandboxed (Untrusted Fork Contribution) |
""")
        return 0

    # 2. Forced GitHub-Hosted Mode
    if args.mode == "github":
        print("[Router] Forced mode 'github' requested.")
        hosted_runner = "windows-latest" if args.required_os == "windows" else "ubuntu-latest"
        reason = "Forced mode: github explicitly requested."
        if args.needs_gpu:
            print("[Router Warning] GPU was requested but hosted CPU runner was selected due to forced github mode.", file=sys.stderr)
            reason += " Warning: Standard GitHub-hosted runners do not provide GPU acceleration."
        set_github_output("target", "github-hosted")
        set_github_output("runner", json.dumps(hosted_runner))
        set_github_output("reason", reason)
        set_github_output("mode", "github")
        set_github_output("waited_seconds", "0")
        set_workflow_summary(f"""### 🌐 ShadowRunner Routing Decision
| Parameter | Value |
| :--- | :--- |
| **Selected Target** | `GitHub-hosted` (`{hosted_runner}`) |
| **Requested Mode** | `github` (Manual Override) |
| **Routing Reason** | {reason} |
""")
        return 0

    # 3. Forced Shadow PC Mode
    if args.mode == "shadow":
        print("[Router] Forced mode 'shadow' requested.")
        shadow_runner = ["self-hosted", "windows", "x64"]
        reason = "Forced mode: shadow explicitly requested."
        set_github_output("target", "shadow-pc")
        set_github_output("runner", json.dumps(shadow_runner))
        set_github_output("reason", reason)
        set_github_output("mode", "shadow")
        set_github_output("waited_seconds", "0")
        set_workflow_summary(f"""### 💻 ShadowRunner Routing Decision
| Parameter | Value |
| :--- | :--- |
| **Selected Target** | `Shadow PC (self-hosted)` (`{shadow_runner}`) |
| **Requested Mode** | `shadow` (Manual Override) |
| **Routing Reason** | {reason} |
""")
        return 0

    # 4. Auto Mode (Check Shadow PC Runner Status)
    if not args.token:
        print("[Router Warning] No ROUTER_PAT provided. Falling back to GitHub-hosted runner for reliability.", file=sys.stderr)
        hosted_runner = "windows-latest" if args.required_os == "windows" else "ubuntu-latest"
        reason = "ROUTER_PAT credentials not available to query runner pool. Safe fallback to GitHub-hosted."
        set_github_output("target", "github-hosted")
        set_github_output("runner", json.dumps(hosted_runner))
        set_github_output("reason", reason)
        set_github_output("mode", "auto")
        set_github_output("waited_seconds", "0")
        set_workflow_summary(f"""### ⚠️ ShadowRunner Routing Decision
| Parameter | Value |
| :--- | :--- |
| **Selected Target** | `GitHub-hosted` (`{hosted_runner}`) |
| **Requested Mode** | `auto` |
| **Routing Reason** | {reason} |
""")
        return 0

    start_time = time.time()
    waited = 0

    while True:
        data = query_org_runners(args.org, args.token)
        if not data or "runners" not in data:
            print("[Router Error] Failed to obtain valid runner list from GitHub API.", file=sys.stderr)
            hosted_runner = "windows-latest" if args.required_os == "windows" else "ubuntu-latest"
            reason = "Failed to query organization runner status via GitHub API. Falling back to GitHub-hosted."
            set_github_output("target", "github-hosted")
            set_github_output("runner", json.dumps(hosted_runner))
            set_github_output("reason", reason)
            set_github_output("mode", "auto")
            set_github_output("waited_seconds", str(int(waited)))
            return 0

        # Locate shadow runner
        matched = None
        for r in data.get("runners", []):
            if r.get("name") == args.runner_name:
                matched = r
                break
        if not matched:
            for r in data.get("runners", []):
                labels = [lbl.get("name") for lbl in r.get("labels", [])]
                if "shadow-pc" in labels or "self-hosted" in labels:
                    matched = r
                    break

        if not matched:
            print(f"[Router] No Shadow runner matching '{args.runner_name}' found in {args.org}.")
            if args.needs_gpu:
                print("[Router Error] Workload requires GPU, but no GPU runner exists. Aborting.", file=sys.stderr)
                set_workflow_summary(f"### ❌ GPU Requirement Unmet\nWorkload requires NVIDIA GPU, but Shadow PC runner `{args.runner_name}` was not found.")
                return 1
            hosted_runner = "windows-latest" if args.required_os == "windows" else "ubuntu-latest"
            reason = f"Shadow PC runner '{args.runner_name}' not found in organization pool. Falling back to GitHub-hosted."
            set_github_output("target", "github-hosted")
            set_github_output("runner", json.dumps(hosted_runner))
            set_github_output("reason", reason)
            set_github_output("mode", "auto")
            set_github_output("waited_seconds", str(int(waited)))
            set_workflow_summary(f"""### 🌐 ShadowRunner Routing Decision
| Parameter | Value |
| :--- | :--- |
| **Selected Target** | `GitHub-hosted` (`{hosted_runner}`) |
| **Requested Mode** | `auto` |
| **Routing Reason** | {reason} |
""")
            return 0

        status = matched.get("status")
        busy = matched.get("busy", False)
        runner_id = matched.get("id")

        print(f"[Router Check] Runner ID {runner_id} ('{matched.get('name')}'): status='{status}', busy={busy}")

        # Check if Offline
        if status != "online":
            print(f"[Router] Shadow runner is {status}.")
            if args.needs_gpu:
                print("[Router Error] Workload requires GPU, but Shadow PC is offline. Standard GitHub-hosted runners have no GPU. Aborting truthfully.", file=sys.stderr)
                set_workflow_summary(f"### ❌ GPU Requirement Unmet\nWorkload requires NVIDIA GPU hardware, but Shadow PC is **offline**.\nStandard GitHub-hosted runners do not provide GPUs. Cannot proceed with false CPU fallback.")
                return 1
            hosted_runner = "windows-latest" if args.required_os == "windows" else "ubuntu-latest"
            reason = f"Shadow PC runner '{matched.get('name')}' is offline. Automatically routed to GitHub-hosted runner."
            set_github_output("target", "github-hosted")
            set_github_output("runner", json.dumps(hosted_runner))
            set_github_output("reason", reason)
            set_github_output("mode", "auto")
            set_github_output("waited_seconds", str(int(waited)))
            set_workflow_summary(f"""### 🌐 ShadowRunner Routing Decision
| Parameter | Value |
| :--- | :--- |
| **Selected Target** | `GitHub-hosted` (`{hosted_runner}`) |
| **Requested Mode** | `auto` |
| **Shadow Status** | 🔴 Offline |
| **Routing Reason** | {reason} |
""")
            return 0

        # Runner is Online and Idle
        if not busy:
            print(f"[Router] Shadow runner is ONLINE and IDLE. Selecting Shadow PC!")
            shadow_runner = ["self-hosted", "windows", "x64"]
            reason = f"Shadow PC runner '{matched.get('name')}' is online and available."
            if waited > 0:
                reason += f" (Waited {int(waited)}s for capacity to free up)"
            set_github_output("target", "shadow-pc")
            set_github_output("runner", json.dumps(shadow_runner))
            set_github_output("reason", reason)
            set_github_output("mode", "auto")
            set_github_output("waited_seconds", str(int(waited)))
            set_workflow_summary(f"""### 💻 ShadowRunner Routing Decision
| Parameter | Value |
| :--- | :--- |
| **Selected Target** | `Shadow PC (self-hosted)` (`{shadow_runner}`) |
| **Requested Mode** | `auto` |
| **Shadow Status** | 🟢 Online (Idle) |
| **Waited Time** | `{int(waited)}s` |
| **Routing Reason** | {reason} |
""")
            return 0

        # Runner is Online but Busy
        waited = time.time() - start_time
        if waited >= args.wait_timeout:
            print(f"[Router] Wait timeout reached ({int(waited)}s >= {args.wait_timeout}s). Falling back to GitHub-hosted.")
            if args.needs_gpu:
                print("[Router Error] Workload requires GPU, but Shadow PC remained busy past timeout. Cannot substitute CPU runner.", file=sys.stderr)
                set_workflow_summary(f"### ❌ GPU Requirement Unmet\nShadow PC remained busy past {args.wait_timeout}s timeout. Workload strictly requires GPU.")
                return 1
            hosted_runner = "windows-latest" if args.required_os == "windows" else "ubuntu-latest"
            reason = f"Shadow PC runner was busy and wait limit ({args.wait_timeout}s) was exceeded. Falling back to GitHub-hosted."
            set_github_output("target", "github-hosted")
            set_github_output("runner", json.dumps(hosted_runner))
            set_github_output("reason", reason)
            set_github_output("mode", "auto")
            set_github_output("waited_seconds", str(int(waited)))
            set_workflow_summary(f"""### ⏱️ ShadowRunner Routing Decision
| Parameter | Value |
| :--- | :--- |
| **Selected Target** | `GitHub-hosted` (`{hosted_runner}`) |
| **Requested Mode** | `auto` |
| **Shadow Status** | 🟡 Online (Busy) |
| **Waited Time** | `{int(waited)}s` (Max Limit Reached) |
| **Routing Reason** | {reason} |
""")
            return 0

        remaining = int(args.wait_timeout - waited)
        print(f"[Router] Runner is currently busy. Waiting {args.poll_interval}s (elapsed: {int(waited)}s, remaining: {remaining}s)...")
        time.sleep(min(args.poll_interval, remaining))

if __name__ == "__main__":
    sys.exit(main())
