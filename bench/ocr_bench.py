# -*- coding: utf-8 -*-
"""RapidOCR 檢驗報告欄位抽取 benchmark（呼叫 ClinicSnap 的辨識模組）。

合成「檢驗報告樣式」版面 × 多種影像品質變體，量測：
身分證抽取正確率（含 checksum 把關）、姓名/生日/病歷號出現率、單張耗時。
結尾自報總測試張數與各欄位命中數。

用法：
    python ocr_bench.py --clinicsnap-src <ClinicSnap clone 的 src 目錄> [PPOCRV6|PPOCRV5]

依賴：pip install rapidocr onnxruntime pillow；另需本機 clone ClinicSnap（不隨本 repo 散布）。
"""
import argparse
import io
import random
import sys
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_CANDIDATES = [
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    # Windows
    "C:/Windows/Fonts/msjh.ttc",
    "C:/Windows/Fonts/mingliu.ttc",
    # Linux（Noto）
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


def load_font(size):
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    raise SystemExit("找不到可用的中文字型，請在 FONT_CANDIDATES 補上本機字型路徑")


NAMES = ["王小明", "陳雅婷", "林承翰", "黃郁涵", "張家豪", "李欣穎"]
LAB_ROWS = [
    ("WBC", "白血球", "8.2", "10^3/uL", "4.0-10.0"),
    ("HGB", "血色素", "12.8", "g/dL", "11.5-15.5"),
    ("PLT", "血小板", "285", "10^3/uL", "150-400"),
    ("AST", "麩胺酸苯醋酸轉胺基酶", "28", "U/L", "10-42"),
    ("ALT", "麩胺酸丙酮酸轉胺基酶", "22", "U/L", "10-40"),
    ("Glucose AC", "飯前血糖", "92", "mg/dL", "70-100"),
    ("HbA1c", "糖化血色素", "5.4", "%", "4.0-6.0"),
    ("Ferritin", "儲鐵蛋白", "45.2", "ng/mL", "22-322"),
]


def make_valid_id(letter_values, weights):
    """產生 checksum 正確的隨機測試用身分證字號（非真實個資）。"""
    letter = random.choice(list(letter_values))
    gender = random.choice("12")
    body = [int(c) for c in gender + "".join(str(random.randint(0, 9)) for _ in range(7))]
    lv = letter_values[letter]
    digits = [lv // 10, lv % 10] + body
    partial = sum(d * w for d, w in zip(digits, weights[:10]))
    check = (10 - partial % 10) % 10
    return letter + "".join(map(str, body)) + str(check)


def render_report(name, pid, dob_text, chart_no):
    """畫一張近似檢驗報告的版面（約 200dpi 半頁）。"""
    w, h = 1654, 1100
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    f_title = load_font(44)
    f_head = load_font(30)
    f_body = load_font(26)

    d.text((w // 2 - 260, 40), "測試醫事檢驗所 檢驗報告單", font=f_title, fill="black")
    d.line([(60, 110), (w - 60, 110)], fill="black", width=3)
    d.text((80, 140), f"姓名：{name}", font=f_head, fill="black")
    d.text((560, 140), f"身分證字號：{pid}", font=f_head, fill="black")
    d.text((80, 195), f"出生日期：{dob_text}", font=f_head, fill="black")
    d.text((560, 195), f"病歷號：{chart_no}", font=f_head, fill="black")
    d.text((1150, 140), "採檢日：2026-07-15", font=f_head, fill="black")
    d.text((1150, 195), "報告日：2026-07-16", font=f_head, fill="black")
    d.line([(60, 250), (w - 60, 250)], fill="black", width=2)

    cols = [80, 320, 760, 980, 1200]
    for x, t in zip(cols, ["項目", "中文名稱", "結果", "單位", "參考值"]):
        d.text((x, 270), t, font=f_head, fill="black")
    y = 330
    for row in LAB_ROWS:
        for x, t in zip(cols, row):
            d.text((x, y), t, font=f_body, fill="black")
        y += 52
    d.text((80, y + 40), "醫檢師：測試員　判讀醫師：測試醫師", font=f_body, fill="black")
    return img


def variants(img):
    """模擬不同取得品質：乾淨、低解析掃描、歪斜、模糊、低品質 JPEG。"""
    out = {"clean": img}
    out["scan100dpi"] = img.resize((img.width // 2, img.height // 2), Image.LANCZOS)
    out["rot2deg"] = img.rotate(2, expand=True, fillcolor="white")
    out["blur"] = img.filter(ImageFilter.GaussianBlur(1.2))
    buf = io.BytesIO()
    img.resize((img.width * 2 // 3, img.height * 2 // 3)).save(buf, "JPEG", quality=45)
    out["jpeg_lowq"] = Image.open(buf).convert("RGB")
    return out


def to_bytes(img):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--clinicsnap-src", default="ClinicSnap/src",
                        help="ClinicSnap clone 內的 src 目錄路徑")
    parser.add_argument("ocr_version", nargs="?", default="PPOCRV6",
                        choices=["PPOCRV6", "PPOCRV5"])
    args = parser.parse_args()

    src = Path(args.clinicsnap_src).resolve()
    if not (src / "clinic_snap").is_dir():
        raise SystemExit(f"找不到 clinic_snap 模組：{src}（請先 clone ClinicSnap 並確認路徑）")
    sys.path.insert(0, str(src))
    from clinic_snap.services.patient_id_ocr import (  # noqa: E402
        _LETTER_VALUES, _WEIGHTS, recognize_patient_id_from_image,
    )

    random.seed(42)
    samples = []
    for i, name in enumerate(NAMES):
        pid = make_valid_id(_LETTER_VALUES, _WEIGHTS)
        dob = (f"{random.randint(100, 114)}/{random.randint(1, 12):02d}/{random.randint(1, 28):02d}"
               if i % 2 == 0 else
               f"20{random.randint(10, 24)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}")
        samples.append((name, pid, dob, f"{random.randint(10000, 99999)}"))

    t0 = time.perf_counter()
    warm = recognize_patient_id_from_image(to_bytes(render_report(*samples[0])), 960, args.ocr_version)
    print(f"engine={warm.backend}  warmup(首次含載模型)={time.perf_counter()-t0:.1f}s\n")

    stats, times = {}, []
    for name, pid, dob, chart in samples:
        for vname, vimg in variants(render_report(name, pid, dob, chart)).items():
            t0 = time.perf_counter()
            res = recognize_patient_id_from_image(to_bytes(vimg), 960, args.ocr_version)
            times.append(time.perf_counter() - t0)
            s = stats.setdefault(vname, [0, 0, 0, 0, 0])
            s[0] += 1
            s[1] += (res.patient_id == pid and res.checksum_valid)
            s[2] += (name in res.full_text)
            s[3] += (dob in res.full_text)
            s[4] += (chart in res.full_text)

    print(f"=== 結果（engine {warm.backend}, det_side_len=960）===")
    print(f"{'變體':<12}{'張數':>4}{'身分證':>8}{'姓名':>7}{'生日':>7}{'病歷號':>8}")
    for vname, (n, a, b, c, e) in stats.items():
        print(f"{vname:<12}{n:>4}{a:>7}/{n}{b:>6}/{n}{c:>6}/{n}{e:>7}/{n}")
    tot = [sum(x) for x in zip(*stats.values())]
    times.sort()
    print(f"\n總測試張數 {tot[0]}：身分證 {tot[1]}/{tot[0]}、姓名 {tot[2]}/{tot[0]}、"
          f"生日 {tot[3]}/{tot[0]}、病歷號 {tot[4]}/{tot[0]}")
    print(f"單張耗時：中位 {times[len(times)//2]*1000:.0f} ms｜"
          f"p95 {times[int(len(times)*0.95)]*1000:.0f} ms｜最慢 {times[-1]*1000:.0f} ms")


if __name__ == "__main__":
    main()
