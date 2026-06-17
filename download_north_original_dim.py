"""
Télécharge thumb_north.jpg (yaw=0, vue nord) pour chaque dossier original_dim.
Lit original_dim_mapping.csv (dir_name,pano_id), sauvegarde dans original_dim/{dir_name}/.
"""
import argparse, os, random, subprocess, time
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DELAY_MIN, DELAY_MAX = 0.5, 1.0
SYNC_EVERY = 300

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
         r"find ~/data/ancres/original_dim -maxdepth 2 -name thumb_north.jpg -printf '%h\n'"
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
        ["rsync", "-az", "--include=*/", "--include=thumb_north.jpg",
         "--exclude=*", "-e", ssh, str(out) + "/",
         f"{gc2_user}@{gc2_ip}:~/data/ancres/original_dim/"],
        capture_output=True, text=True, timeout=600
    )
    if result.returncode != 0:
        print(f"  rsync push warning: {result.stderr[:300]}")
    return result.returncode == 0

def download_north(dir_name, pano_id, out_dir, session):
    d = out_dir / dir_name
    if (d / "thumb_north.jpg").exists():
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
            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
            return "ok"
        return "fail"
    except Exception:
        return "fail"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start",    type=int, required=True)
    ap.add_argument("--count",    type=int, required=True)
    ap.add_argument("--mapping",  default="original_dim_mapping.csv")
    ap.add_argument("--gc2-user", default=os.environ.get("GC2_USER", ""))
    ap.add_argument("--gc2-ip",   default=os.environ.get("GC2_IP", ""))
    ap.add_argument("--gc2-key",  default="/tmp/gc2_key")
    args = ap.parse_args()

    gc2_ok = bool(args.gc2_user and args.gc2_ip and Path(args.gc2_key).exists())

    rows = []
    for line in open(args.mapping):
        parts = line.strip().split(",")
        if len(parts) == 2:
            rows.append((parts[0], parts[1]))
    batch = rows[args.start: args.start + args.count]
    print(f"OriginalDim north [{args.start}..{args.start+len(batch)-1}] — {len(batch)} dirs")

    out = Path("output_orig")
    out.mkdir(exist_ok=True)

    if gc2_ok:
        print("Récupération thumb_north.jpg déjà présents sur gc2...")
        done_gc2 = get_done_on_gc2(args.gc2_user, args.gc2_ip, args.gc2_key)
        already = sum(1 for (dn, _) in batch if dn in done_gc2)
        # Créer fichiers locaux pour les skip
        for dir_name, _ in batch:
            if dir_name in done_gc2:
                d = out / dir_name
                d.mkdir(parents=True, exist_ok=True)
                (d / "thumb_north.jpg").write_bytes(b"")  # placeholder
        print(f"  {already} déjà faits → skippés")

    session = make_session()
    ok = fail = skip = 0

    for i, (dir_name, pano_id) in enumerate(batch):
        res = download_north(dir_name, pano_id, out, session)
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
