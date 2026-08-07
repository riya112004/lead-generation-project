"""Run the API and trigger VISIBLE Google AI mode automation for a query.

Usage (from the src/ folder):
    python run.py "mobile stores in himachal"
    python run.py --max 12 "it companies in chandigarh"

Chrome opens VISIBLY on your screen, goes to Google, types the query, presses
Enter, switches to AI Mode, captures the AI answer, then visits each result
page - you watch the scraping live.

Uses a persistent Chrome profile (.chrome_profile/) so Google does not block
it - sign in to Google once the first time it opens.
"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))


def _wait_health(port: str, timeout: float = 30.0) -> bool:
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3):
                return True
        except Exception:
            time.sleep(0.5)
    return False


def main() -> None:
    args = list(sys.argv[1:])
    max_results = 8
    if "--max" in args:
        i = args.index("--max")
        try:
            max_results = int(args.pop(i + 1))
        except ValueError:
            pass
        args.pop(i)

    query = " ".join(args).strip() or os.environ.get("QUERY", "").strip()
    if not query:
        print('Usage: python run.py "<query>" [--max N]')
        sys.exit(1)

    port = os.environ.get("PORT", "3001")
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.main:app", "--port", port],
        cwd=BASE,
    )
    try:
        if not _wait_health(port):
            print(f"[run] Server did not start on port {port}. Aborting.")
            sys.exit(1)

        url = (
            f"http://127.0.0.1:{port}/search/auto"
            f"?q={urllib.parse.quote(query)}"
            f"&max_results={max_results}"
        )
        print(f"[run] Starting visible Google AI mode automation for: {query!r}")
        print("[run] >>> Chrome is opening now - watch the Google AI search happen <<<")
        with urllib.request.urlopen(url, timeout=3600) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        results = data.get("results", [])
        print("\n" + "=" * 60)
        print(f"QUERY: {data.get('query')}   engine: {data.get('engine')}")
        ai = data.get("ai_overview") or ""
        if ai:
            print("\n--- GOOGLE AI ANSWER ---")
            print(ai[:1500] + ("..." if len(ai) > 1500 else ""))
        print(f"\nTOTAL LEADS: {data.get('total_results', len(results))}")
        for i, lead in enumerate(results, 1):
            print(f"  {i}. {lead.get('business_name')}  |  {lead.get('website')}")
            print(f"     email={lead.get('email') or '-'}  phone={lead.get('phone') or '-'}")
        if data.get("errors"):
            print(f"\nErrors ({len(data['errors'])}):")
            for err in data["errors"][:5]:
                print(f"  - {err}")

        print("\n[run] Server still running on http://127.0.0.1:%s" % port)
        print("[run] Press Ctrl+C to stop.")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[run] Stopping.")
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    main()
