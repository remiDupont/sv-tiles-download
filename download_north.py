"""
Télécharge 1 thumbnail yaw=0 (vue nord géographique) par pano_id.
1 requête/pano, ~150KB, délai 0.5-1.0s.
Sortie: ancres_sans_API/{pano_id}/thumb_north.jpg + .north_done
"""
import argparse, os, random, subprocess, time
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DELAY_MIN, DELAY_MAX = 0.5, 1.0
SYNC_EVERY = 500

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

def make_session():
    s = requests.Session()
    s.mount("https://", HTTPAdapter(
        max_retries=Retry(total=4, backoff_factor=2,
                          status_forcelist=[429, 500, 502, 503, 504]),
        pool_maxsize=4))
    s.headers.update({
        "User-Agent": random.choice(USER_AGENTS),
        "Referer": "https://www.google.com/maps/",
    })
    return s

def get_done_on_gc2(gc2_user, gc2_ip, key_path):
    result = subprocess.run(
        ["ssh", "-i", key_path, "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=30",
         f"{gc2_user}@{gc2_ip}",
         r"find ~/data/ancres/ancres_sans_API -maxdepth 2 -name .north_done -printf '%h\n'"
         r" | sed 's|.*/||'"],
        capture_output=True, text=True, timeout=300
    )
    if result.returncode != 0:
        print(f"  SSH find warning: {result.stderr[:200]}")
        return set()
    return set(result.stdout.strip().split('\n'))

def rsync_push(out, gc2_user, gc2_ip, key_path):
    ssh = f"ssh -i {key_path} -o StrictHostKeyChecking=no -o ConnectTimeout=30"
    result = subprocess.run(
        ["rsync", "-az", "--include=*/", "--include=thumb_north.jpg", "--include=.north_done",
         "--exclude=*", "-e", ssh, str(out) + "/",
         f"{gc2_user}@{gc2_ip}:~/data/ancres/ancres_sans_API/"],
        capture_output=True, text=True, timeout=600
    )
    if result.returncode != 0:
        print(f"  rsync push warning: {result.stderr[:300]}")
    return result.returncode == 0

def download_north(pano_id, out_dir, session):
    d = out_dir / pano_id
    if (d / ".north_done").exists():
        return "skip"
    d.mkdir(parents=True, exist_ok=True)

    try:
        r = session.get(
            "https://streetviewpixels-pa.googleapis.com/v1/thumbnail",
            params=dict(cb_client="maps_sv.tactile",
                        panoid=pano_id, yaw=0, pitch=0, w=640, h=640),
            timeout=20
        )
        if r.ok and r.headers.get("content-type", "").startswith("image"):
            (d / "thumb_north.jpg").write_bytes(r.content)
            (d / ".north_done").write_text("ok")
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
            return "ok"
        else:
            (d / ".north_fail").write_text(f"{r.status_code}")
            return "fail"
    except Exception as e:
        (d / ".north_fail").write_text(str(e)[:100])
        return "fail"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start",    type=int, required=True)
    ap.add_argument("--count",    type=int, required=True)
    ap.add_argument("--panos",    default="pano_ids.txt")
    ap.add_argument("--gc2-user", default=os.environ.get("GC2_USER", ""))
    ap.add_argument("--gc2-ip",   default=os.environ.get("GC2_IP", ""))
    ap.add_argument("--gc2-key",  default="/tmp/gc2_key")
    args = ap.parse_args()

    gc2_ok = bool(args.gc2_user and args.gc2_ip and Path(args.gc2_key).exists())

    ids = Path(args.panos).read_text().splitlines()
    batch = ids[args.start: args.start + args.count]
    print(f"North batch [{args.start}..{args.start+len(batch)-1}] — {len(batch)} panos")

    out = Path("output")
    out.mkdir(exist_ok=True)

    if gc2_ok:
        print("Récupération .north_done depuis gc2...")
        done_gc2 = get_done_on_gc2(args.gc2_user, args.gc2_ip, args.gc2_key)
        already = 0
        for pid in batch:
            if pid in done_gc2:
                d = out / pid
                d.mkdir(parents=True, exist_ok=True)
                (d / ".north_done").write_text("ok")
                already += 1
        print(f"  {already} déjà faits → skippés")

    session = make_session()
    ok = fail = skip = 0

    for i, pid in enumerate(batch):
        res = download_north(pid, out, session)
        if res == "ok":     ok += 1
        elif res == "fail": fail += 1
        else:               skip += 1

        if gc2_ok and ok > 0 and ok % SYNC_EVERY == 0:
            print(f"  [{i+1}/{len(batch)}] ok={ok} fail={fail} skip={skip} — sync gc2...")
            rsync_push(out, args.gc2_user, args.gc2_ip, args.gc2_key)

        if (i + 1) % 100 == 0:
            print(f"  [{i+1}/{len(batch)}] ok={ok} fail={fail} skip={skip}")

    print(f"Terminé: ok={ok} fail={fail} skip={skip} — sync final...")
    if gc2_ok:
        rsync_push(out, args.gc2_user, args.gc2_ip, args.gc2_key)

if __name__ == "__main__":
    main()
