#!/usr/bin/env python3
"""
wildlife - identify animals in gardecam media with MegaDetector + SpeciesNet.

Two-stage pipeline, run inside a local Docker image (CUDA when the machine
has an NVIDIA GPU, CPU otherwise):
  1. MegaDetector finds animals (it is trained on millions of day + IR
     trail-camera frames, so it handles night footage far better than
     COCO-trained models).
  2. SpeciesNet classifies each find into one of ~2,500 species - raccoon,
     Virginia opossum, domestic cat, domestic dog, and so on.
Both ship in the `speciesnet` package (google/cameratrapai).

For each media file it writes a sidecar `<name>.wildlife.json` next to the
original, and for every species found it saves an annotated best frame under
`<media>/annotated/`. Files whose sidecar was already produced by this engine
are skipped, so it is safe to re-run after `gardecam.py sync` - only new clips
(and clips scanned by an older engine) are processed.

Usage:
  wildlife.py [--dir DIR] [--force] [--limit N] [--stride N] [--score S]
              [--country CC] [--remote] [--report]

  --dir DIR      media directory (default: $GARDECAM_MEDIA or ./media)
  --force        reprocess files that already have a current sidecar
  --limit N      only process the first N pending files (for a quick test)
  --stride N     analyze every Nth video frame (default 5)
  --score S      minimum ensemble score to count a frame (default 0.3)
  --country CC   3-letter country code for SpeciesNet geofencing, e.g. USA
                 (default: $GARDECAM_COUNTRY; improves species rollups)
  --all          also record humans and vehicles (ignored by default -
                 only wildlife is reported)
  --remote       run the scan on $GARDECAM_REMOTE over ssh instead: rsync the
                 media there, run this script on that machine (using its GPU
                 if it has one), and rsync the sidecars + annotated frames back
  --report       print a summary of existing sidecars and exit (no docker)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
IMAGE_BASE = "gardecam-wildlife"
DOCKERFILE = os.path.join(HERE, "Dockerfile.wildlife")
CACHE = os.path.join(os.path.expanduser("~"), ".cache", "gardecam-yolo")
SIDECAR_SUFFIX = ".wildlife.json"
ANNOTATED_SUBDIR = "annotated"
ENGINE = "megadetector+speciesnet"


def _load_env(path=None):
    """Read KEY=value lines from .env. Real environment variables win."""
    path = path or os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_env()

# The container-side worker. Kept inline so this stays a single runnable file;
# it is written to a temp dir and mounted into the container at run time.
WORKER = r'''
import json, os, shutil, sys, tempfile
from pathlib import Path

import cv2
from speciesnet import DEFAULT_MODEL, SpeciesNet

cfg = json.load(open("/work/job.json"))
media = Path("/media")
annotated = media / cfg["annotated_subdir"]

print(f"loading model {DEFAULT_MODEL} (downloads to cache on first run)...",
      flush=True)
model = SpeciesNet(DEFAULT_MODEL)


def label_of(prediction):
    """Common name from a SpeciesNet taxonomy string.

    Predictions look like 'uuid;class;order;family;genus;species;common name'.
    When the classifier is unsure the ensemble rolls up to a higher taxon and
    the tail segments are empty, so take the last non-empty one.
    """
    parts = [p for p in prediction.split(";") if p]
    return parts[-1].lower() if len(parts) > 1 else prediction.lower()


def extract_frames(src, outdir, stride):
    cap = cv2.VideoCapture(str(src))
    paths, i = [], 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if i % stride == 0:
            h, w = frame.shape[:2]
            if w > 1280:
                frame = cv2.resize(frame, (1280, int(h * 1280 / w)))
            p = outdir / f"f{i:06d}.jpg"
            cv2.imwrite(str(p), frame)
            paths.append(p)
        i += 1
    cap.release()
    return paths


def draw(frame_path, detections, label, text):
    img = cv2.imread(str(frame_path))
    if img is None:
        return None
    h, w = img.shape[:2]
    # MegaDetector boxes animals, humans, AND vehicles, but the species
    # caption applies to the whole frame - so only draw the boxes of the
    # kind being labeled, or parked cars end up boxed under a "cat" caption.
    wanted = label if label in ("human", "vehicle") else "animal"
    for d in detections or []:
        if d.get("label") != wanted or d.get("conf", 0) < 0.2:
            continue
        x, y, bw, bh = d.get("bbox", [0, 0, 0, 0])
        p1 = (int(x * w), int(y * h))
        p2 = (int((x + bw) * w), int((y + bh) * h))
        cv2.rectangle(img, p1, p2, (0, 255, 0), 2)
    cv2.putText(img, text, (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                (0, 255, 0), 2, cv2.LINE_AA)
    return img


for name in cfg["files"]:
    src = media / name
    is_video = src.suffix.lower() == ".mp4"
    stride = cfg["stride"] if is_video else 1
    tmpdir = Path(tempfile.mkdtemp(prefix="frames_"))
    try:
        frames = extract_frames(src, tmpdir, stride)
        if not frames:
            print(f"ERROR {name}: could not read any frames", flush=True)
            continue
        instances = [{"filepath": str(p)} for p in frames]
        if cfg.get("country"):
            for inst in instances:
                inst["country"] = cfg["country"]
        preds = model.predict(instances_dict={"instances": instances})

        stats = {}  # label -> {"frames": n, "max_score": s, "prediction": str}
        best = {}   # label -> (score, frame path, detections)
        analyzed = 0
        for p in preds.get("predictions", []):
            analyzed += 1
            pred = p.get("prediction") or ""
            score = float(p.get("prediction_score") or 0)
            label = label_of(pred)
            if label in ("blank", "no cv result") or score < cfg["score"]:
                continue
            if label in ("human", "vehicle") and not cfg.get("all"):
                continue
            s = stats.setdefault(label, {"frames": 0, "max_score": 0.0,
                                         "prediction": pred})
            s["frames"] += 1
            if score > s["max_score"]:
                s["max_score"] = score
                s["prediction"] = pred
            if label not in best or score > best[label][0]:
                best[label] = (score, p.get("filepath"), p.get("detections"))

        # A single hit in a whole video is usually noise; require two sampled
        # frames for videos, one for still photos.
        need = 2 if is_video else 1
        present = sorted(l for l, s in stats.items() if s["frames"] >= need)

        for label in present:
            score, fpath, dets = best[label]
            img = draw(fpath, dets, label, f"{label} {score:.2f}")
            if img is not None:
                annotated.mkdir(exist_ok=True)
                safe = label.replace(" ", "_").replace("/", "_")
                cv2.imwrite(str(annotated / f"{src.stem}_{safe}.jpg"), img)

        sidecar = {
            "file": name,
            "engine": cfg["engine"],
            "params": {"stride": stride, "score": cfg["score"],
                       "country": cfg.get("country")},
            "frames_analyzed": analyzed,
            "species": stats,
            "present": present,
        }
        tmp = media / (name + cfg["sidecar_suffix"] + ".part")
        tmp.write_text(json.dumps(sidecar, indent=2))
        os.replace(tmp, media / (name + cfg["sidecar_suffix"]))
        print(f"DONE {name}: {', '.join(present) if present else 'nothing'}",
              flush=True)
    except Exception as e:
        print(f"ERROR {name}: {e}", flush=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
'''


def media_files(mdir):
    return sorted(
        f for f in os.listdir(mdir)
        if f.lower().endswith((".mp4", ".jpg", ".jpeg"))
    )


def has_current_sidecar(mdir, name):
    sc = os.path.join(mdir, name + SIDECAR_SUFFIX)
    if not os.path.exists(sc):
        return False
    try:
        with open(sc) as f:
            return json.load(f).get("engine") == ENGINE
    except (json.JSONDecodeError, OSError):
        return False


def pending_files(mdir, force):
    return [n for n in media_files(mdir)
            if force or not has_current_sidecar(mdir, n)]


def report(mdir):
    """Summarize existing sidecars: which clips have animals, and totals."""
    rows, totals, unscanned = [], {}, 0
    for name in media_files(mdir):
        sc = os.path.join(mdir, name + SIDECAR_SUFFIX)
        if not os.path.exists(sc):
            unscanned += 1
            continue
        with open(sc) as f:
            data = json.load(f)
        present = data.get("present", [])
        if present:
            scores = data.get("species", data.get("detections", {}))
            desc = ", ".join(
                f"{c} ({scores.get(c, {}).get('max_score', scores.get(c, {}).get('max_conf', 0)):.2f})"
                for c in present
            )
            rows.append((name, desc))
        for c in present:
            totals[c] = totals.get(c, 0) + 1
    if rows:
        print("\nfiles with detections:")
        for name, desc in rows:
            print(f"  {name}: {desc}")
    print("\ntotals:")
    if totals:
        for c, n in sorted(totals.items(), key=lambda kv: -kv[1]):
            print(f"  {c}: {n} file(s)")
    else:
        print("  no animals detected in any scanned file")
    scanned = len(media_files(mdir)) - unscanned
    print(f"\n{scanned} file(s) scanned, {unscanned} not yet scanned")


def gpu_available():
    """True when this machine can run CUDA containers."""
    if not shutil.which("nvidia-smi"):
        return False
    if subprocess.run(["nvidia-smi", "-L"], capture_output=True).returncode:
        return False
    r = subprocess.run(["docker", "info", "--format", "{{json .Runtimes}}"],
                       capture_output=True, text=True)
    return "nvidia" in r.stdout


def ensure_image(gpu):
    image = f"{IMAGE_BASE}:{'gpu' if gpu else 'cpu'}"
    r = subprocess.run(["docker", "image", "inspect", image],
                       capture_output=True)
    if r.returncode == 0:
        return image
    print(f"building docker image {image} (first run only, takes a few "
          "minutes)...")
    cmd = ["docker", "build", "-t", image, "-f", DOCKERFILE]
    if gpu:
        cmd += ["--build-arg", "TORCH_INDEX=https://pypi.org/simple"]
    r = subprocess.run(cmd + [HERE])
    if r.returncode != 0:
        raise SystemExit("docker build failed")
    return image


def run_remote(args, mdir):
    """Push media to $GARDECAM_REMOTE, scan there, pull the results back."""
    host = os.environ.get("GARDECAM_REMOTE", "").strip()
    if not host:
        raise SystemExit(
            "no remote configured: set GARDECAM_REMOTE=<ssh host> in .env")
    rdir = os.environ.get("GARDECAM_REMOTE_DIR", "gardecam-wildlife").rstrip("/")
    for tool in ("ssh", "rsync"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} is required for --remote")

    print(f"remote scan on {host} (dir ~/{rdir})")
    if subprocess.run(["ssh", host, f"mkdir -p {rdir}/media"]).returncode:
        raise SystemExit(f"cannot ssh to {host}")

    print("pushing script + media (first push moves the whole archive; "
          "later ones only the new clips)...")
    push = subprocess.run(
        ["rsync", "-a", "--info=stats1",
         os.path.join(HERE, "wildlife.py"), DOCKERFILE,
         f"{host}:{rdir}/"])
    media = subprocess.run(
        ["rsync", "-a", "--info=progress2", mdir + "/", f"{host}:{rdir}/media/"])
    if push.returncode or media.returncode:
        raise SystemExit("rsync to remote failed")

    cmd = (f"cd {rdir} && python3 wildlife.py --dir media"
           f" --stride {args.stride} --score {args.score}")
    if args.country:
        cmd += f" --country {args.country}"
    if args.all:
        cmd += " --all"
    if args.force:
        cmd += " --force"
    if args.limit:
        cmd += f" --limit {args.limit}"
    r = subprocess.run(["ssh", host, cmd])
    if r.returncode:
        raise SystemExit(f"remote scan failed (exit {r.returncode})")

    print("pulling sidecars + annotated frames back...")
    pull = subprocess.run(
        ["rsync", "-a", "--info=stats1",
         "--include=*/", "--include=*" + SIDECAR_SUFFIX,
         "--include=annotated/**", "--exclude=*",
         f"{host}:{rdir}/media/", mdir + "/"])
    if pull.returncode:
        raise SystemExit("rsync from remote failed")
    report(mdir)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--dir", default=os.environ.get(
        "GARDECAM_MEDIA", os.path.join(HERE, "media")))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--stride", type=int, default=5)
    ap.add_argument("--score", type=float, default=0.3)
    ap.add_argument("--country", default=os.environ.get("GARDECAM_COUNTRY", ""))
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--remote", action="store_true")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    mdir = os.path.abspath(args.dir)
    if not os.path.isdir(mdir):
        raise SystemExit(f"media directory not found: {mdir}")
    if args.report:
        report(mdir)
        return
    if args.remote:
        run_remote(args, mdir)
        return
    if not shutil.which("docker"):
        raise SystemExit("docker is required but not installed")

    files = pending_files(mdir, args.force)
    if args.limit:
        files = files[: args.limit]
    if not files:
        print("nothing to do - every media file already has a sidecar "
              "(use --force to redo)")
        report(mdir)
        return
    gpu = gpu_available()
    image = ensure_image(gpu)
    print(f"{len(files)} file(s) to scan in {mdir} with {ENGINE} "
          f"({'CUDA' if gpu else 'CPU'})")

    os.makedirs(CACHE, exist_ok=True)
    with tempfile.TemporaryDirectory() as work:
        with open(os.path.join(work, "job.json"), "w") as f:
            json.dump({
                "files": files,
                "stride": args.stride,
                "score": args.score,
                "country": args.country or None,
                "all": args.all,
                "engine": ENGINE,
                "sidecar_suffix": SIDECAR_SUFFIX,
                "annotated_subdir": ANNOTATED_SUBDIR,
            }, f)
        with open(os.path.join(work, "worker.py"), "w") as f:
            f.write(WORKER)

        cmd = [
            "docker", "run", "--rm", "--ipc=host",
            *(["--gpus", "all"] if gpu else []),
            "--user", f"{os.getuid()}:{os.getgid()}",
            # HOME points at the cache so model weights persist across runs
            # instead of re-downloading every time.
            "-e", "HOME=/cache", "-w", "/cache",
            "-v", f"{CACHE}:/cache",
            "-v", f"{os.path.abspath(mdir)}:/media",
            "-v", f"{work}:/work:ro",
            image, "python", "/work/worker.py",
        ]
        r = subprocess.run(cmd)
        if r.returncode != 0:
            raise SystemExit(f"docker run failed (exit {r.returncode})")

    report(mdir)


if __name__ == "__main__":
    main()
