"""
Télécharge les tiles SV (zoom=2, 8 req/pano) pour un batch de pano_ids.
- Sync initial depuis gc2 : récupère les .done existants → skip automatique
- Rsync progressif toutes les SYNC_EVERY panos → survie aux crashes runner
Args: --start INT --count INT --panos pano_ids.txt
"""
import argparse, concurrent.futures, io, os, random, subprocess, time
from pathlib import Path
import requests
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ZOOM, COLS, ROWS = 2, 4, 2
DELAY_MIN, DELAY_MAX = 1.0, 2.5
TILE_THREADS = 8
SYNC_EVERY = 200

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
        pool_maxsize=16))
    s.headers.update({
        "User-Agent": random.choice(USER_AGENTS),
        "Referer": "https://www.google.com/maps/",
    })
    return s

def get_done_on_gc2(gc2_user, gc2_ip, key_path):
    """Liste les pano_ids déjà faits sur gc2 via SSH find (texte seul, rapide)."""
    result = subprocess.run(
        ["ssh", "-i", key_path, "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=30",
         f"{gc2_user}@{gc2_ip}",
         r"find ~/data/ancres/ancres_sans_API -maxdepth 2 -name .done -printf '%h\n'"
         r" | sed 's|.*/||'"],
        capture_output=True, text=True, timeout=300
    )
    if result.returncode != 0:
        print(f"  SSH find warning: {result.stderr[:200]}")
        return set()
    return set(result.stdout.strip().split('\n'))

def rsync_push(out, gc2_user, gc2_ip, key_path):
    ssh = f"ssh -i {key_path} -o StrictHostKeyChecking=no -o ConnectTimeout=30"
    remote = f"{gc2_user}@{gc2_ip}:~/data/ancres/ancres_sans_API/"
    result = subprocess.run(
        ["rsync", "-az", "-e", ssh, str(out) + "/", remote],
        capture_output=True, text=True, timeout=600
    )
    if result.returncode != 0:
        print(f"  rsync push warning: {result.stderr[:300]}")
    return result.returncode == 0

def download_pano(pano_id, out_dir, session):
    d = out_dir / pano_id
    if (d / ".done").exists():
        return "skip"
    d.mkdir(parents=True, exist_ok=True)

    def get_tile(xy):
        x, y = xy
        url = (f"https://streetviewpixels-pa.googleapis.com/v1/tile"
               f"?cb_client=maps_sv.tactile&panoid={pano_id}&x={x}&y={y}&zoom={ZOOM}")
        r = session.get(url, timeout=20)
        if r.ok and r.headers.get("content-type", "").startswith("image"):
            return (x, y), Image.open(io.BytesIO(r.content)).convert("RGB")
        return (x, y), None

    coords = [(x, y) for y in range(ROWS) for x in range(COLS)]
    tiles = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=TILE_THREADS) as ex:
        for xy, img in ex.map(get_tile, coords):
            if img: tiles[xy] = img

    if len(tiles) < COLS * ROWS:
        (d / ".fail").write_text(f"{len(tiles)}/8")
        return "fail"

    full = Image.new("RGB", (512 * COLS, 512 * ROWS))
    for (x, y), t in tiles.items():
        full.paste(t, (x * 512, y * 512))
    full.save(d / "pano_equirect.jpg", quality=90, optimize=True)
    (d / ".done").write_text("ok")
    time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
    return "ok"

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
    print(f"Batch [{args.start}..{args.start+len(batch)-1}] — {len(batch)} panos")

    out = Path("output")
    out.mkdir(exist_ok=True)

    # ── Sync initial : liste les .done sur gc2, crée les fichiers locaux ──
    if gc2_ok:
        print("Récupération des panos déjà faits sur gc2 (SSH find)...")
        done_gc2 = get_done_on_gc2(args.gc2_user, args.gc2_ip, args.gc2_key)
        already = 0
        for pid in batch:
            if pid in done_gc2:
                d = out / pid
                d.mkdir(parents=True, exist_ok=True)
                (d / ".done").write_text("ok")
                already += 1
        print(f"  {already} déjà faits sur gc2 → skippés")

    session = make_session()
    ok = fail = skip = 0

    for i, pid in enumerate(batch):
        res = download_pano(pid, out, session)
        if res == "ok":     ok += 1
        elif res == "fail": fail += 1
        else:               skip += 1

        # ── Rsync progressif toutes les SYNC_EVERY réussites ─────────────
        if gc2_ok and ok > 0 and ok % SYNC_EVERY == 0:
            print(f"  [{i+1}/{len(batch)}] ok={ok} fail={fail} skip={skip} — sync gc2...")
            rsync_push(out, args.gc2_user, args.gc2_ip, args.gc2_key)

        if (i + 1) % 50 == 0:
            print(f"  [{i+1}/{len(batch)}] ok={ok} fail={fail} skip={skip}")

    # ── Sync final ────────────────────────────────────────────────────────
    print(f"Terminé: ok={ok} fail={fail} skip={skip} — sync final gc2...")
    if gc2_ok:
        rsync_push(out, args.gc2_user, args.gc2_ip, args.gc2_key)

if __name__ == "__main__":
    main()
