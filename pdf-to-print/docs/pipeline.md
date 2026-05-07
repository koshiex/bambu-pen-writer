# Pipeline — техническая документация тулчейна

Описание скриптов в `scripts/`, темплейтов в `templates/`, и зачем именно так.

Не для оператора (см. [`operator-manual.md`](operator-manual.md)) — для разработчика, который будет дорабатывать.

---

## Карта файлов: куда что положить

| Источник | Назначение | Когда применяется |
|---|---|---|
| **`output/notebook.gcode`** | **Никуда не копируется**, шлётся в принтер через **Orca → Device → Open G-code File** | Финальный артефакт |
| `templates/bambu_start.gcode` | Опционально в Orca Printer Settings → Machine G-code | При **Slice** в Orca (для нашего pipeline не нужно) |
| `templates/bambu_end.gcode` | Опционально туда же | При **Slice** |
| `templates/page_pause.gcode` | Никуда не вставляется — используется `merge_pages.py` | Внутри pipeline |
| `templates/vpype_profile.toml` | Референс/шаблон, **не копировать вручную** | `~/.vpype.toml` авто-генерируется `svg_to_gcode.py` с подставленными `Z_PEN_DOWN`/`Z_HOP` |

`output/notebook.gcode` **self-contained**:
```
[bambu_start.gcode  — UMTS init, wipe, bed leveling, install pause]
;===== PAGE 01 =====
[page_01.gcode]
[page_pause.gcode (NEXT_PAGE=02)]
;===== PAGE 02 =====
[page_02.gcode]
... (всего 24 страницы, 23 page-flip pauses)
;===== PAGE 24 =====
[page_24.gcode]
[bambu_end.gcode  — final pause + safe shutdown]
```

При отправке через **Device → Open G-code File** Orca **не модифицирует** файл — start G-code из printer profile НЕ применяется (он применяется только при Slice). Поэтому дубля нет.

⚠️ **НЕ открывать `notebook.gcode` через File → Open / drag-and-drop / Slice** — Orca попытается обработать как проект и сломает поток.

---

## Архитектура (поток данных)

```
pdfs/2.pdf (24 страницы handwriting font, page 165×205 mm portrait)
    │
    ├── scripts/extract_pages.sh
    │       inkscape --pages=N --export-text-to-path --export-filename=...
    │       page count detection: mdls (macOS) | python regex fallback
    ▼
build/svg/page_NN.svg (24 файла, ~4-5 MB каждый, vector text-as-paths)
    │
    ├── scripts/svg_to_gcode.py (uses .venv/bin/vpype)
    │       vpype read → pagerotate (CCW 90°)
    │            → scale -o 0 0 -- 1 -1 (Y flip, SVG-down → Bambu-up)
    │            → translate 24mm 217mm (paper origin in landscape pen-frame)
    │            → linemerge --tolerance 0.1mm + linesort
    │            → gwrite -p bambu_p1s_umts (см. templates/vpype_profile.toml)
    ▼
build/gcode/page_NN.gcode (24 файла, ~5-7 MB каждый, чистые G0/G1+Z-hop, без headers)
    │
    ├── scripts/merge_pages.py
    │       templates/bambu_start.gcode
    │     + ;===== PAGE 01 ===== + page_01.gcode
    │     + templates/page_pause.gcode (with NEXT_PAGE=02)
    │     + ;===== PAGE 02 ===== + page_02.gcode
    │     + ... (23 паузы между 24 страницами)
    │     + templates/bambu_end.gcode
    ▼
output/notebook.gcode (~141 MB, отправляется в Orca → P1S)
```

---

## Файлы и за что отвечают

### `scripts/extract_pages.sh`

PDF → SVG. Аргументы: `$1=PDF` (default `pdfs/2.pdf`), `$2=out_dir` (default `build/svg`).

Тонкости:
- `inkscape --pages=N --export-text-to-path` — критично, иначе Inkscape оставляет text-элементы для отдельных страниц где есть «доступные» глифы → vpype не любит text, будет потеря содержимого
- Page count detection: `mdls -name kMDItemNumberOfPages -raw` → fallback на Python regex поиска `/Type /Page` маркеров в бинарнике PDF
- Inkscape флаг `--pdf-page=N` НЕ существует в 1.4+ — правильно `--pages=N`

### `scripts/inspect_svg.py`

Диагностический скрипт — печатает viewBox, размер в мм, count элементов, vector vs raster heuristic, превью первых path. Запускать вручную для проверки нового PDF:
```bash
python3 scripts/inspect_svg.py build/inspect/page_01.svg
```

Inkscape экспортирует SVG width/height без unit (user units = px @ 96 dpi). Скрипт корректно конвертирует px → mm.

### `scripts/svg_to_gcode.py`

Главный конвертер. Запускает vpype для каждого SVG. Параметры в начале файла:
```python
PAPER_ORIGIN_X = "24mm"     # paper bottom-left X в landscape pen-frame
PAPER_ORIGIN_Y = "217mm"    # paper TOP Y (после scale 1 -1 Y становится отрицательным,
                            # translate шифтит на 217 чтобы content оказался Y=52..217)
Z_PEN_DOWN = 18.0           # nozzle Z when pen touches paper (UMTS pen sticks ~18mm below nozzle)
Z_HOP = 3.0                 # additional Z lift between strokes (Z-hop)
VPYPE_PROFILE = "bambu_p1s_umts"
LINEMERGE_TOLERANCE = "0.1mm"
```

При запуске скрипт **перезаписывает `~/.vpype.toml`** с подставленными значениями `Z_PEN_DOWN` и `Z_HOP`. Не редактировать `~/.vpype.toml` вручную — будет затёрт при следующем запуске.

Чтобы изменить Z-калибровку:
1. Поправить `Z_PEN_DOWN` в `scripts/svg_to_gcode.py`
2. Запустить `./scripts/build.sh` — пере-сгенерируется gcode со скорректированным Z

**Почему такой transform**:
- SVG: portrait 165×205 mm, Y down (top of page = Y=0)
- `pagerotate` (CCW): становится 205×165 landscape, content повёрнут
- `scale -o 0 0 -- 1 -1`: Y инвертирован относительно (0,0). Pen-frame Y up.
- `translate 24mm 217mm`: content перемещается в paper bounds X=24..229, Y=52..217

Bounds итогового G-code (pen-frame): X≈28.6..221, Y≈57..211 — внутри paper и UMTS safe zone.

### `scripts/merge_pages.py`

Конкатенирует start + (page + pause) × N + end в один G-code. `{NEXT_PAGE}` placeholder в `page_pause.gcode` заменяется на двузначный номер.

### `scripts/build.sh`

Orchestrator. Активирует .venv, синхронизирует `~/.vpype.toml` из `templates/`, запускает 3 фазы.

---

## Templates

### `templates/bambu_start.gcode`

Скопирован из `docs/umts-p1s-pen.md` §"Start G-code (P1S)" с подставленными значениями переменных Orca:
- `{nozzle_temperature_initial_layer[initial_no_support_extruder]-20}` → `160`
- `{nozzle_temperature_initial_layer}` → `180`
- `{first_layer_print_min[0]}` → `24`, `{first_layer_print_min[1]}` → `52`
- `{first_layer_print_size[0]}` → `205`, `{first_layer_print_size[1]}` → `165`
- `{if scan_first_layer} ... {endif}` → блок удалён (false)

Дополнительно в конце добавлена «UMTS install pause» — пауза для установки модуля с ручкой перед началом первой страницы.

### `templates/page_pause.gcode`

Что выполняется на каждой паузе между страницами:
1. Z lift на 20 мм (clear notebook)
2. Park head front-center (X=128, Y=20) для удобного доступа через дверцу
3. `M400` flush motion buffer
4. `M400 U1` — Bambu interactive pause
5. `M109 S180` — re-assert nozzle temp при resume

Маркер `;===== PAGE {NEXT_PAGE} =====` — placeholder заменяется в `merge_pages.py`.

### `templates/bambu_end.gcode`

Минимальный safe shutdown. Главное — пауза `M400 U1` перед auto-cut filament чтобы оператор успел снять модуль.

### `templates/vpype_profile.toml`

`gwrite` профиль для vpype-gcode. Ключевые поля:
- `unit = "mm"` — выход в миллиметрах
- `vertical_flip = false` — Y orientation handled in pipeline (`scale 1 -1`), не в gwrite
- `segment_first` — травел на начало path (G0 F18000) + опускание ручки (G1 Z0)
- `segment` — рисование (G1 X Y F12000 = 200 мм/с)
- `line_end` — Z-hop вверх (G1 Z3) после path

**Скорости**:
- Travel `F18000` = 300 мм/с
- Draw `F12000` = 200 мм/с

Для гелевой ручки 200 мм/с может быть слишком быстро (размазывание). Снижается правкой `segment` строки в profile (например F9000 = 150 мм/с) и пере-копированием в `~/.vpype.toml` через `cp templates/vpype_profile.toml ~/.vpype.toml` или `./scripts/build.sh` (синкает автоматом).

---

## Как добавить новое

### Новый PDF

```bash
# 1. Положить в pdfs/
cp /path/new.pdf pdfs/new.pdf

# 2. Запустить с указанием
./scripts/build.sh pdfs/new.pdf output/new_notebook.gcode

# 3. Если у нового PDF другой формат страницы (не 165×205):
#    отредактировать PAPER_ORIGIN_X / Y в svg_to_gcode.py
```

### Другая ручка

1. Откалибровать Z-offset (Test 3) → новое значение в Orca printer profile
2. Если ручка медленнее (POSCA-style маркер) — снизить F12000 → F2000-3000 в `templates/vpype_profile.toml`
3. Перегенерировать через `./scripts/build.sh`

### Зеркалирование чётных страниц (mirror margins)

Сейчас не реализовано. Если потребуется — добавить в `svg_to_gcode.py`:

```python
# Псевдокод
if int(page_num) % 2 == 0:  # четная
    args.extend(["scale", "-o", "0", "0", "--", "-1", "1"])  # mirror X
    args.extend(["translate", "205mm", "0"])                  # сдвинуть обратно
```

Это псевдо-код — порядок трансформов важен и зависит от того, ДО или ПОСЛЕ pagerotate применяем зеркалирование. Тестировать на конкретном PDF.

### Pause command не работает (M400 U1 ignored)

Заменить в `templates/page_pause.gcode` и `bambu_start.gcode`:
```
M400 U1  →  M0
```

После M0 принтер ждёт нажатия Resume на LCD/Bambu Handy. M0 совместим с Marlin firmware, должен работать на P1S.

---

## Известные ограничения

- **Размер G-code ~141 MB**. Прошивка P1S и Orca могут долго парсить. Печать стартует не сразу. Альтернатива — стримить через MQTT напрямую (не реализовано).
- **Толщина вёрстки тетради** не учтена в G-code. Z-offset калибруется один раз с тетрадью на столе (Test 3).
- **Подгиб левой части тетради** требует физического действия оператора на каждой паузе. Невозможно автоматизировать без доп. железа.
- **Корешок тетради создаёт неровность** ~10 мм от сгиба. Линии в этой зоне могут быть искажены.
- **Python 3.14 несовместим** с vpype (pkg_resources broken). Используем 3.13.
- **Inkscape >= 1.4 обязателен** — флаг `--pages=N` появился в этой версии (раньше был `--pdf-page=N`).
