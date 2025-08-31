# dedupe_phrases.py
import json, re, os
SRC = "phrases.json"
DST = "phrases_clean.json"
REP = "phrases_dups_report.txt"

# убрать никуд (огласовки) и «мусорные» символы для сравнения
NIKUD_RE = re.compile(r"[\u0591-\u05C7]")  # огласовки и кантилляция
TRIM_RE = re.compile(r"[`׳'\"“”]+")

def norm_he(s: str) -> str:
    s = s or ""
    s = NIKUD_RE.sub("", s)
    s = TRIM_RE.sub("", s)
    s = s.strip()
    return s

def main():
    if not os.path.exists(SRC):
        raise SystemExit(f"Нет файла {SRC}")
    with open(SRC, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise SystemExit("phrases.json должен быть списком объектов")

    groups = {}
    for row in data:
        he = (row.get("he") or "").strip()
        ru = (row.get("ru") or "").strip()
        note = (row.get("note") or "").strip()
        key = norm_he(he)
        if not key:
            continue
        g = groups.setdefault(key, {"he": he, "ru_set": set(), "note_set": set(), "samples": []})
        if ru: g["ru_set"].add(ru)
        if note: g["note_set"].add(note)
        g["samples"].append(row)

    cleaned = []
    report_lines = []
    for key, g in groups.items():
        ru_join = "; ".join(sorted(g["ru_set"]))
        note_join = "; ".join(sorted({n for n in g["note_set"] if n}))
        cleaned.append({"he": g["he"], "ru": ru_join, "note": note_join})
        if len(g["samples"]) > 1:
            report_lines.append(f"• {g['he']}  --> {ru_join}")

    # стабильно отсортируем по ивриту
    cleaned.sort(key=lambda x: norm_he(x["he"]))

    with open(DST, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, ensure_ascii=False, indent=2)

    with open(REP, "w", encoding="utf-8") as f:
        if report_lines:
            f.write("Найдены и слиты дубли (одинаковый he, разные ru):\n")
            f.write("\n".join(report_lines))
        else:
            f.write("Дублей не найдено.\n")

    print(f"✅ Готово: {DST} ({len(cleaned)} фраз)")
    print(f"📝 Отчёт: {REP}")

if __name__ == "__main__":
    main()
