"""
Télécharge les orthophotos IGN pour chaque pano dans ancres_sans_API.
Lit pano_coords.csv (pano_id,lat,lon), télécharge ortho_large.jpg + ortho_serre.jpg.
Marqueur de complétion : .ortho_done (indépendant de .done pour les tiles).
"""
import argparse, math, os, subprocess
from pathlib import Path
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

IGN_WMS   = "https://data.geopf.fr/wms-r/wms"
IGN_LAYER = "ORTHOIMAGERY.ORTHOPHOTOS"
ORTHO     = {"large": 100.0, "serre": 30.0}  # demi-côté en mètres → 200m / 60m
SYNC_EVERY = 500

def wgs84_to_3857(lat, lon):
    x = lon * 20037508.34 / 180
    y = math.log(math.tan((90 + lat) * math.pi / 360)) / (math.pi / 180)
    y = y * 20037508.34 / 180
    return x, y

def make_session():
    s = requests.Session()
    s.mount("https://", HTTPAdapter(
        max_retries=Retry(total=4, backoff_factor=2,
                          status_forcelist=[429, 500, 502, 503, 504]),
        pool_maxsize=8))
    return s

def get_ortho_done_on_gc2(gc2_user, gc2_ip, key_path):
    result = subprocess.run(
        ["ssh", "-i", key_path, "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=30",
         f"{gc2_user}@{gc2_ip}",
         r"find ~/data/ancres/ancres_sans_API -maxdepth 2 -name .ortho_done -printf '%h\n'"
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
        ["rsync", "-az", "-e", ssh, str(out) + "/",
         f"{gc2_user}@{gc2_ip}:~/data/ancres/ancres_sans_API/"],
        capture_output=True, text=True, timeout=600
    )
    if result.returncode != 0:
        print(f"  rsync push warning: {result.stderr[:300]}")
    return result.returncode == 0

def download_ortho(pano_id, lat, lon, out_dir, session):
    d = out_dir / pano_id
    if (d / ".ortho_done").exists():
        return "skip"
    d.mkdir(parents=True, exist_ok=True)

    x, y = wgs84_to_3857(lat, lon)
    ok = 0
    for name, half in ORTHO.items():
        out_file = d / f"ortho_{name}.jpg"
        if out_file.exists():
            ok += 1
            continue
        try:
            r = session.get(IGN_WMS, params=dict(
                SERVICE="WMS", VERSION="1.3.0", REQUEST="GetMap",
                LAYERS=IGN_LAYER, STYLES="", CRS="EPSG:3857",
                BBOX=f"{x-half},{y-half},{x+half},{y+half}",
                WIDTH=512, HEIGHT=512, FORMAT="image/jpeg"
            ), timeout=30)
            if r.ok and r.headers.get("content-type", "").startswith("image"):
                out_file.write_bytes(r.content)
                ok += 1
        except Exception as e:
            print(f"  {pano_id} ortho_{name}: {e}")

    if ok == 2:
        (d / ".ortho_done").write_text("ok")
        return "ok"
    return "fail"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start",    type=int, required=True)
    ap.add_argument("--count",    type=int, required=True)
    ap.add_argument("--coords",   default="pano_coords.csv")
    ap.add_argument("--gc2-user", default=os.environ.get("GC2_USER", ""))
    ap.add_argument("--gc2-ip",   default=os.environ.get("GC2_IP", ""))
    ap.add_argument("--gc2-key",  default="/tmp/gc2_key")
    args = ap.parse_args()

    gc2_ok = bool(args.gc2_user and args.gc2_ip and Path(args.gc2_key).exists())

    rows = []
    with open(args.coords) as f:
        for line in f:
            parts = line.strip().split(",")
            if len(parts) == 3:
                rows.append((parts[0], float(parts[1]), float(parts[2])))
    batch = rows[args.start: args.start + args.count]
    print(f"Ortho batch [{args.start}..{args.start+len(batch)-1}] — {len(batch)} panos")

    out = Path("output")
    out.mkdir(exist_ok=True)

    if gc2_ok:
        print("Récupération des orthos déjà faites sur gc2 (SSH find)...")
        done_gc2 = get_ortho_done_on_gc2(args.gc2_user, args.gc2_ip, args.gc2_key)
        already = 0
        for pano_id, _, __ in batch:
            if pano_id in done_gc2:
                d = out / pano_id
                d.mkdir(parents=True, exist_ok=True)
                (d / ".ortho_done").write_text("ok")
                already += 1
        print(f"  {already} déjà faits sur gc2 → skippés")

    session = make_session()
    ok = fail = skip = 0

    for i, (pano_id, lat, lon) in enumerate(batch):
        res = download_ortho(pano_id, lat, lon, out, session)
        if res == "ok":     ok += 1
        elif res == "fail": fail += 1
        else:               skip += 1

        if gc2_ok and ok > 0 and ok % SYNC_EVERY == 0:
            print(f"  [{i+1}/{len(batch)}] ok={ok} fail={fail} skip={skip} — sync gc2...")
            rsync_push(out, args.gc2_user, args.gc2_ip, args.gc2_key)

        if (i + 1) % 100 == 0:
            print(f"  [{i+1}/{len(batch)}] ok={ok} fail={fail} skip={skip}")

    print(f"Terminé: ok={ok} fail={fail} skip={skip} — sync final gc2...")
    if gc2_ok:
        rsync_push(out, args.gc2_user, args.gc2_ip, args.gc2_key)

if __name__ == "__main__":
    main()
