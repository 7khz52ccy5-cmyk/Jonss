#!/usr/bin/env python3
"""TikTok-Schnitt-Pipeline (ffmpeg-basiert).

Macht aus rohen Alltagsclips ein fertiges 9:16-Video:
  1. Clips normalisieren (1080x1920, 30fps, Hochformat-Crop oder Blur-Hintergrund)
  2. Stille / Pausen rausschneiden (Jump Cuts)  -> --cut-silence
  3. Clips aneinanderhängen (optional jeden Clip auf max. Länge kürzen)
  4. Ton normalisieren (laut & gleichmäßig wie auf TikTok üblich)
  5. Hook-Text in den ersten Sekunden          -> --hook "..."
  6. Automatische Untertitel (Wort-für-Wort)    -> --captions (braucht faster-whisper)
  7. Musik drunterlegen, leiser unter Sprache   -> --music datei.mp3

Beispiel:
  python3 edit.py footage/tag1/*.mp4 -o exports/tag1.mp4 \
      --cut-silence --captions --hook "Mein erster Tag am Bondi Beach 🌊" \
      --music footage/musik/chill.mp3
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

W, H, FPS = 1080, 1920, 30
FONT = "Inter Display"  # fett, gut lesbar; Fallback macht libass selbst


def run(cmd, quiet=True):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"Fehler bei: {' '.join(cmd)}\n{r.stderr[-2000:]}")
    return r


def probe(path):
    out = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_streams", "-show_format", path]).stdout
    info = json.loads(out)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    rot = 0
    if v:
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = abs(int(sd["rotation"]))
        rot = abs(int(v.get("tags", {}).get("rotate", rot)))
    w, h = (int(v["width"]), int(v["height"])) if v else (W, H)
    if rot in (90, 270):
        w, h = h, w
    return {"w": w, "h": h, "has_audio": a is not None,
            "dur": float(info["format"].get("duration", 0))}


def normalize(src, dst, mode, max_len):
    """Bringt jeden Clip auf 1080x1920/30fps/AAC – egal ob Hoch- oder Querformat."""
    p = probe(src)
    if mode == "blur" and p["w"] > p["h"]:
        vf = (f"[0:v]split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,"
              f"crop={W}:{H},boxblur=30:5[bg];[b]scale={W}:-2[fg];"
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps={FPS},setsar=1,format=yuv420p[v]")
    else:  # crop: füllt den Bildschirm komplett (Standard bei Handy-Hochkant)
        vf = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
              f"crop={W}:{H},fps={FPS},setsar=1,format=yuv420p[v]")
    cmd = ["ffmpeg", "-y", "-i", src]
    if not p["has_audio"]:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    cmd += ["-filter_complex", vf, "-map", "[v]",
            "-map", "0:a" if p["has_audio"] else "1:a",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "192k", "-shortest"]
    if max_len:
        cmd += ["-t", str(max_len)]
    run(cmd + [dst])


def silence_segments(src, noise_db, min_sil, pad):
    """Liefert die Abschnitte MIT Ton (Sprache/Action) als [(start, ende), ...]."""
    r = subprocess.run(["ffmpeg", "-i", src, "-af",
                        f"silencedetect=noise={noise_db}dB:d={min_sil}", "-f", "null", "-"],
                       capture_output=True, text=True)
    dur = probe(src)["dur"]
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    keep, cur = [], 0.0
    for s, e in zip(starts, ends + [dur] * (len(starts) - len(ends))):
        if s - cur > 0.15:
            keep.append((max(0, cur - pad), min(dur, s + pad)))
        cur = e
    if dur - cur > 0.15:
        keep.append((max(0, cur - pad), dur))
    return keep or [(0, dur)]


def cut_silence(src, dst, noise_db, min_sil, pad):
    segs = silence_segments(src, noise_db, min_sil, pad)
    if len(segs) == 1 and segs[0][0] <= 0.05:
        shutil.copy(src, dst)
        return 0.0
    parts = []
    for i, (s, e) in enumerate(segs):
        parts.append(f"[0:v]trim={s:.3f}:{e:.3f},setpts=PTS-STARTPTS[v{i}];"
                     f"[0:a]atrim={s:.3f}:{e:.3f},asetpts=PTS-STARTPTS[a{i}];")
    concat = "".join(f"[v{i}][a{i}]" for i in range(len(segs)))
    fc = "".join(parts) + f"{concat}concat=n={len(segs)}:v=1:a=1[v][a]"
    run(["ffmpeg", "-y", "-i", src, "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k", dst])
    return probe(src)["dur"] - probe(dst)["dur"]


def concat(files, dst, tmp):
    lst = os.path.join(tmp, "list.txt")
    with open(lst, "w") as f:
        for p in files:
            f.write(f"file '{os.path.abspath(p)}'\n")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", dst])


def ass_time(t):
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def ass_escape(t):
    # libass kann keine Farb-Emojis -> rauswerfen (Emojis gehören in die TikTok-Caption)
    t = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]", "", t).strip()
    return t.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


ASS_HEADER = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{FONT},78,&H00FFFFFF,&H0000E5FF,&H00000000,&H64000000,1,0,0,0,100,100,0,0,1,6,2,2,80,80,620,1
Style: Hook,{FONT},84,&H00000000,&H00FFFFFF,&H00FFFFFF,&H00FFFFFF,1,0,0,0,100,100,0,0,3,22,0,8,90,90,330,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def transcribe(src, lang, model_size):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        sys.exit("Für --captions bitte zuerst installieren:  pip install faster-whisper")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(src, language=lang, word_timestamps=True, vad_filter=True)
    words = []
    for seg in segments:
        for w in seg.words or []:
            words.append((w.start, w.end, w.word.strip()))
    return words


def caption_events(words, per_line=3):
    """Gruppiert Wörter zu kurzen Häppchen (2–3 Wörter) und hebt das gesprochene Wort gelb hervor."""
    ev, chunk = [], []
    for w in words:
        chunk.append(w)
        if len(chunk) >= per_line or re.search(r"[.!?,]$", w[2]):
            ev.append(chunk)
            chunk = []
    if chunk:
        ev.append(chunk)
    lines = []
    for ch in ev:
        for i, (s, e, _) in enumerate(ch):
            end = ch[i + 1][0] if i + 1 < len(ch) else e
            txt = " ".join(
                (r"{\c&H0000E5FF&\fscx110\fscy110}" + ass_escape(t.upper()) + r"{\r}")
                if j == i else ass_escape(t.upper())
                for j, (_, _, t) in enumerate(ch))
            lines.append(f"Dialogue: 0,{ass_time(s)},{ass_time(end)},Cap,,0,0,0,,{txt}")
    return lines


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clips", nargs="+", help="Rohclips in der gewünschten Reihenfolge")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--mode", choices=["crop", "blur"], default="crop",
                    help="Querformat: crop = zoomen, blur = unscharfer Hintergrund")
    ap.add_argument("--max-clip", type=float, default=0, help="Jeden Clip auf max. N Sekunden kürzen")
    ap.add_argument("--cut-silence", action="store_true", help="Pausen rausschneiden (Jump Cuts)")
    ap.add_argument("--silence-db", type=float, default=-32)
    ap.add_argument("--silence-min", type=float, default=0.45, help="Pausen ab N Sekunden schneiden")
    ap.add_argument("--silence-pad", type=float, default=0.08)
    ap.add_argument("--hook", default="", help="Text-Overlay in den ersten Sekunden")
    ap.add_argument("--hook-dur", type=float, default=2.8)
    ap.add_argument("--captions", action="store_true", help="Automatische Untertitel (faster-whisper)")
    ap.add_argument("--lang", default="de")
    ap.add_argument("--whisper-model", default="small")
    ap.add_argument("--music", default="", help="Hintergrundmusik (mp3/m4a/wav)")
    ap.add_argument("--music-vol", type=float, default=0.18)
    ap.add_argument("--keep-tmp", action="store_true")
    a = ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="ttedit_")
    try:
        prepared = []
        for i, c in enumerate(a.clips):
            n = os.path.join(tmp, f"n{i:03d}.mp4")
            print(f"[1/5] Clip {i + 1}/{len(a.clips)} vorbereiten: {os.path.basename(c)}")
            normalize(c, n, a.mode, a.max_clip)
            if a.cut_silence:
                s = os.path.join(tmp, f"s{i:03d}.mp4")
                saved = cut_silence(n, s, a.silence_db, a.silence_min, a.silence_pad)
                print(f"      -> {saved:.1f}s Pause rausgeschnitten")
                n = s
            prepared.append(n)

        print("[2/5] Clips zusammenfügen")
        joined = os.path.join(tmp, "joined.mp4")
        concat(prepared, joined, tmp)
        total = probe(joined)["dur"]

        events = []
        if a.hook:
            events.append(f"Dialogue: 1,{ass_time(0)},{ass_time(min(a.hook_dur, total))},Hook,,0,0,0,,"
                          + r"{\fad(120,200)}" + ass_escape(a.hook))
        if a.captions:
            print("[3/5] Untertitel erzeugen (Whisper)")
            words = transcribe(joined, a.lang, a.whisper_model)
            if a.hook:  # Untertitel nicht über den Hook legen
                words = [w for w in words if w[0] >= a.hook_dur - 0.3] or words
            events += caption_events(words)
            with open(os.path.splitext(a.out)[0] + ".transkript.txt", "w") as f:
                f.write(" ".join(w[2] for w in words))
        else:
            print("[3/5] Untertitel übersprungen")

        print("[4/5] Text, Ton & Musik mischen")
        vf = "[0:v]null[v]"
        if events:
            ass = os.path.join(tmp, "subs.ass")
            with open(ass, "w") as f:
                f.write(ASS_HEADER + "\n".join(events) + "\n")
            vf = f"[0:v]ass='{ass}'[v]"
        cmd = ["ffmpeg", "-y", "-i", joined]
        if a.music:
            cmd += ["-stream_loop", "-1", "-i", a.music]
            af = (f"[0:a]loudnorm=I=-14:TP=-1.5:LRA=11,asplit[voice][key];"
                  f"[1:a]volume={a.music_vol},afade=t=out:st={max(0, total - 1.5):.2f}:d=1.5[m];"
                  f"[m][key]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[duck];"
                  f"[voice][duck]amix=inputs=2:duration=first:normalize=0[a]")
        else:
            af = "[0:a]loudnorm=I=-14:TP=-1.5:LRA=11[a]"
        os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
        run(cmd + ["-filter_complex", vf + ";" + af, "-map", "[v]", "-map", "[a]",
                   "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "medium", "-crf", "19",
                   "-profile:v", "high", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                   "-c:a", "aac", "-b:a", "192k", "-ar", "48000", a.out])
        print(f"[5/5] Fertig: {a.out}  ({total:.1f}s)")
    finally:
        if not a.keep_tmp:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
