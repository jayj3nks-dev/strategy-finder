#!/usr/bin/env python3
"""
ShadowRunner CI Watchdog
Monitors a self-hosted workload job for:
1. Stuck in queue (runner went offline before pickup or capacity timeout reached).
2. Infrastructure crash during execution (runner disconnected / lost communication).
If an infrastructure interruption occurs, cancels the stuck attempt and dispatches
an automatic fallback run on GitHub-hosted infrastructure for the exact commit SHA.
"""

import os
import sys
import json
import time
import argparse
import urllib.request
import urllib.error

def gh_api_request(url, token, method="GET", data=None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ShadowRunner-Watchdog"
    }
    encoded_data = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=encoded_data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content) if content else {}
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="ignore")
        print(f"[Watchdog API Error] {method} {url} returned HTTP {e.code}: {err_msg}", file=sys.stderr)
        return {"error": e.code, "message": err_msg}
    except Exception as e:
        print(f"[Watchdog Request Error] {e}", file=sys.stderr)
        return {"error": str(e)}

def is_superseded(org, repo, ref, commit_sha, token):
    url = f"https://api.github.com/repos/{org}/{repo}/commits/{ref}"
    resp = gh_api_request(url, token)
    if "sha" in resp and resp["sha"] != commit_sha:
        print(f"[Watchdog] Branch '{ref}' has moved to newer commit {resp['sha']} (tested commit was {commit_sha}).")
        return True
    return False

def trigger_hosted_fallback(org, repo, workflow_file, ref, commit_sha, run_id, token, reason):
    print("==================================================")
    print(f"🚨 TRIGGERING AUTOMATIC HOSTED FALLBACK RECOVERY")
    print(f"Reason: {reason}")
    print(f"Original Run ID: {run_id}")
    print(f"Target Commit:   {commit_sha}")
    print(f"Target Ref:      {ref}")
    print("==================================================")

    dispatch_url = f"https://api.github.com/repos/{org}/{repo}/actions/workflows/{workflow_file}/dispatches"
    payload = {
        "ref": ref,
        "inputs": {
            "mode": "github",
            "fallback_attempt": "1",
            "original_run_id": str(run_id),
            "target_commit": commit_sha
        }
    }
    resp = gh_api_request(dispatch_url, token, method="POST", data=payload)
    if "error" in resp:
        print(f"[Watchdog Error] Failed to dispatch hosted fallback: {resp}", file=sys.stderr)
        return False
    print("[Watchdog] Hosted fallback workflow dispatched successfully.")

    # Cancel the old stuck run so it does not linger in the queue for 24 hours
    cancel_url = f"https://api.github.com/repos/{org}/{repo}/actions/runs/{run_id}/cancel"
    cancel_resp = gh_api_request(cancel_url, token, method="POST")
    print(f"[Watchdog] Original stuck run #{run_id} cancelled.")
    return True

def main():
    parser = argparse.ArgumentParser(description="ShadowRunner CI Watchdog")
    parser.add_argument("--org", default="jayj3nks-dev", help="GitHub Organization")
    parser.add_argument("--repo", required=True, help="Repository name")
    parser.add_argument("--run-id", required=True, type=int, help="Workflow Run ID")
    parser.add_argument("--token", default=os.environ.get("ROUTER_PAT", ""), help="GitHub PAT")
    parser.add_argument("--workload-job-name", default="workload", help="Workload job name to watch")
    parser.add_argument("--workflow-file", default="ci.yml", help="Workflow filename")
    parser.add_argument("--commit-sha", required=True, help="Exact commit SHA")
    parser.add_argument("--ref", required=True, help="Git branch or ref")
    parser.add_argument("--pickup-timeout", type=int, default=300, help="Max seconds to wait for job pickup")
    parser.add_argument("--poll-interval", type=int, default=15, help="Poll interval in seconds")
    parser.add_argument("--runner-name", default="shadow-org-runner", help="Shadow runner name")
    args = parser.parse_args()

    if not args.token:
        print("[Watchdog Warning] ROUTER_PAT is missing. Watchdog cannot monitor run.", file=sys.stderr)
        return 0

    print("==================================================")
    print(" SHADOWRUNNER CI ACTIVE WATCHDOG INITIALIZED")
    print("==================================================")
    print(f"Target Run ID:      {args.run_id}")
    print(f"Workload Job:       {args.workload_job_name}")
    print(f"Commit SHA:         {args.commit_sha}")
    print(f"Ref:                {args.ref}")
    print(f"Pickup Timeout:     {args.pickup_timeout}s")
    print("--------------------------------------------------")

    start_time = time.time()
    job_was_in_progress = False
    in_progress_offline_checks = 0

    while True:
        time.sleep(args.poll_interval)
        elapsed = time.time() - start_time

        # 1. Query Run Jobs
        jobs_url = f"https://api.github.com/repos/{args.org}/{args.repo}/actions/runs/{args.run_id}/jobs"
        jobs_data = gh_api_request(jobs_url, args.token)
        if not jobs_data or "jobs" not in jobs_data:
            print(f"[Watchdog] Could not retrieve jobs list (elapsed: {int(elapsed)}s)...")
            continue

        workload_job = None
        for j in jobs_data.get("jobs", []):
            if args.workload_job_name.lower() in j.get("name", "").lower():
                workload_job = j
                break

        if not workload_job:
            print(f"[Watchdog] Workload job '{args.workload_job_name}' not found yet in run #{args.run_id}...")
            continue

        job_status = workload_job.get("status")
        job_conclusion = workload_job.get("conclusion")
        print(f"[Watchdog Check] Workload job #{workload_job.get('id')}: status='{job_status}', conclusion='{job_conclusion}' (elapsed: {int(elapsed)}s)")

        # Case A: Job is Queued
        if job_status == "queued":
            # Check if Shadow runner is offline
            runners_url = f"https://api.github.com/orgs/{args.org}/actions/runners"
            runners_data = gh_api_request(runners_url, args.token)
            shadow_offline = True
            if runners_data and "runners" in runners_data:
                for r in runners_data.get("runners", []):
                    if r.get("name") == args.runner_name and r.get("status") == "online":
                        shadow_offline = False
                        break

            if shadow_offline or elapsed >= args.pickup_timeout:
                trigger_reason = f"Shadow runner '{args.runner_name}' went offline while job was queued" if shadow_offline else f"Job pickup timeout exceeded ({int(elapsed)}s >= {args.pickup_timeout}s)"
                
                # Check for newer commit
                if is_superseded(args.org, args.repo, args.ref, args.commit_sha, args.token):
                    print(f"[Watchdog] Commit {args.commit_sha} has been superseded. Cancelling obsolete run without recovery.")
                    cancel_url = f"https://api.github.com/repos/{args.org}/{args.repo}/actions/runs/{args.run_id}/cancel"
                    gh_api_request(cancel_url, args.token, method="POST")
                    return 0

                trigger_hosted_fallback(
                    org=args.org,
                    repo=args.repo,
                    workflow_file=args.workflow_file,
                    ref=args.ref,
                    commit_sha=args.commit_sha,
                    run_id=args.run_id,
                    token=args.token,
                    reason=trigger_reason
                )
                return 0

        # Case B: Job is In Progress
        elif job_status == "in_progress":
            job_was_in_progress = True
            # Query runner status to detect mid-execution crash/disconnect
            runners_url = f"https://api.github.com/orgs/{args.org}/actions/runners"
            runners_data = gh_api_request(runners_url, args.token)
            shadow_offline = False
            if runners_data and "runners" in runners_data:
                for r in runners_data.get("runners", []):
                    if r.get("name") == args.runner_name and r.get("status") != "online":
                        shadow_offline = True
                        break

            if shadow_offline:
                in_progress_offline_checks += 1
                print(f"[Watchdog Warning] Shadow runner reported OFFLINE while job is in_progress! (check {in_progress_offline_checks}/2)")
                if in_progress_offline_checks >= 2:
                    print("[Watchdog] Runner disconnected/crashed mid-execution. Triggering automatic hosted fallback.")
                    if is_superseded(args.org, args.repo, args.ref, args.commit_sha, args.token):
                        print(f"[Watchdog] Commit {args.commit_sha} superseded. Cancelling run without recovery.")
                        cancel_url = f"https://api.github.com/repos/{args.org}/{args.repo}/actions/runs/{args.run_id}/cancel"
                        gh_api_request(cancel_url, args.token, method="POST")
                        return 0

                    trigger_hosted_fallback(
                        org=args.org,
                        repo=args.repo,
                        workflow_file=args.workflow_file,
                        ref=args.ref,
                        commit_sha=args.commit_sha,
                        run_id=args.run_id,
                        token=args.token,
                        reason="Shadow runner disconnected / crashed during job execution"
                    )
                    return 0
            else:
                in_progress_offline_checks = 0
            continue

        # Case C: Job is Completed
        elif job_status == "completed":
            print(f"[Watchdog] Workload job completed with conclusion: '{job_conclusion}'.")
            if job_conclusion == "success":
                print("[Watchdog] Workload succeeded on Shadow PC. No recovery needed.")
                return 0

            if job_conclusion in ["cancelled", "skipped"]:
                print(f"[Watchdog] Workload was {job_conclusion}. No recovery needed.")
                return 0

            if job_conclusion == "failure":
                # Check if it was an infrastructure interruption (runner lost communication / crash)
                # or a genuine test failure
                is_infra_failure = False
                steps = workload_job.get("steps", [])
                for s in steps:
                    step_name = s.get("name", "").lower()
                    step_conclusion = s.get("conclusion")
                    if step_conclusion == "failure":
                        if "set up runner" in step_name or "complete job" in step_name:
                            is_infra_failure = True
                            break

                # Also inspect runner status
                runners_url = f"https://api.github.com/orgs/{args.org}/actions/runners"
                runners_data = gh_api_request(runners_url, args.token)
                runner_is_offline = False
                if runners_data and "runners" in runners_data:
                    for r in runners_data.get("runners", []):
                        if r.get("name") == args.runner_name and r.get("status") != "online":
                            runner_is_offline = True
                            break

                if is_infra_failure or runner_is_offline:
                    print("[Watchdog] Infrastructure interruption detected! Runner crashed or lost connection during execution.")
                    if not is_superseded(args.org, args.repo, args.ref, args.commit_sha, args.token):
                        trigger_hosted_fallback(
                            org=args.org,
                            repo=args.repo,
                            workflow_file=args.workflow_file,
                            ref=args.ref,
                            commit_sha=args.commit_sha,
                            run_id=args.run_id,
                            token=args.token,
                            reason="Shadow runner crashed / lost communication during execution"
                        )
                    return 0
                else:
                    print("[Watchdog] Failure was a genuine test or build failure. Not retrying code errors. Truthful result preserved.")
                    return 0

if __name__ == "__main__":
    sys.exit(main())
