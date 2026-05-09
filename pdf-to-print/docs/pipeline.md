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
    ├── scripts/extract_pages.sh          (vector — явный выбор или USE_VECTOR_EXTRACT=1)
    │       inkscape --pages=N --export-text-to-path --export-filename=...
    │       page count: mdls (macOS) | python regex fallback
    │
    ├── scripts/extract_pages_raster.sh    (default в build.sh)
    │       inkscape --pages=N --export-type=png --export-dpi=DPI
    │       → по умолчанию png_to_skeleton_svg.py (скелет штриха = одна линия)
    │       → или TRACE_MODE=potrace: mkbitmap → potrace → normalize_traced_svg.py
    ▼
build/svg/page_NN.svg (vector paths для vpype)
    │
    ├── scripts/svg_to_gcode.py (vpype_cli.execute + сортировка штрихов в Python)
    │       read --quantization 0.05mm --single-layer → pagerotate --clockwise (CW 90°)
    │            → scale -o 0 0 -- 1 -1 (Y flip; итог: gcode_x=y_svg+50.46, gcode_y=x_svg+87.9)
    │            → translate PAPER_ORIGIN_* (paper → nozzle frame)
    │            → по умолчанию в build.sh: --skip-linemerge (см. PDF_TO_PRINT_LINEMERGE); иначе linemerge 0.05mm
    │            → сортировка штрихов (axis=x): строки по убывающему gcode_X (верхняя первой),
    │              внутри строки по min(gcode_Y) (левый край первым)
    │            → ориентация вершин: каждая полилиния разворачивается левым концом вперёд
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

### `scripts/extract_pages_raster.sh`

PDF → PNG → SVG. По умолчанию **`TRACE_MODE=skeleton`**: [`scripts/png_to_skeleton_svg.py`](scripts/png_to_skeleton_svg.py) бинаризует страницу, строит **скелет** (`skimage.morphology.skeletonize`) и выпускает **полилинии по центру штриха**. Так убирается эффект «двух контуров», когда толстый штрих в растре — это **кольцо пикселей**: potrace обводил **внешний и внутренний край** кольца.

Режим **`TRACE_MODE=potrace`**: PNG → ImageMagick → mkbitmap → potrace → [`normalize_traced_svg.py`](scripts/normalize_traced_svg.py) — контур силуэта; на кольцевых штрихах снова возможны два контура.

Зависимости: `inkscape`; `.venv` с **numpy, scikit-image, networkx, Pillow**. Для potrace-режима дополнительно: `potrace`, `mkbitmap`, ImageMagick.

Переменные окружения: `TRACE_MODE`, `EXPORT_DPI` (default 300); для potrace — `MKBITMAP_OPTS`, `POTRACE_OPTS`.

Запуск через orchestrator: по умолчанию raster (`./scripts/build.sh`). Векторный экспорт Inkscape: `./scripts/build.sh … vector` или `USE_VECTOR_EXTRACT=1`.

Компромиссы: скелет даёт «ручку по середине» штриха, не идеальный офсетный контур; много коротких отрезков → крупнее SVG/G-code; очень тонкий текст может ломаться при морфологии — тогда поднять `EXPORT_DPI` или вернуться к `TRACE_MODE=potrace` для эксперимента.

### `scripts/inspect_svg.py`

Диагностический скрипт — печатает viewBox, размер в мм, count элементов, vector vs raster heuristic, превью первых path. Запускать вручную для проверки нового PDF:
```bash
python3 scripts/inspect_svg.py build/inspect/page_01.svg
```

Inkscape экспортирует SVG width/height без unit (user units = px @ 96 dpi). Скрипт корректно конвертирует px → mm.

### `scripts/svg_to_gcode.py`

Главный конвертер. Запускает vpype для каждого SVG. Параметры в начале файла (реальные имена — в [`scripts/svg_to_gcode.py`](scripts/svg_to_gcode.py)):
```python
PAPER_ORIGIN_X / PAPER_ORIGIN_Y   # из PAPER_* и PEN_OFFSET_* (nozzle frame)
Z_PEN_DOWN = 18.0
Z_HOP = 15.0
READ_QUANTIZATION = "0.05mm"
LINEMERGE_TOLERANCE = "0.05mm"
VPYPE_PROFILE = "bambu_p1s_umts"
READING_SORT_INVERT_Y = False  # irrelevant для оси x (default); только для force_axis="y"
READING_ROW_GAP_BREAK_MM = 4.5
READING_ROW_AXIS_RATIO = 0.45
READING_ROW_AXIS_AUTO = False  # см. PDF_TO_PRINT_READING_AXIS_AUTO
```

**Геометрия координат (корень сортировки):** после полной цепочки трансформов `pagerotate CW → scale 1 -1 → translate` получается:
```
gcode_x = 255.46 − y_svg   (строки текста → убывающий gcode_X; строка 1 сверху = наибольший X)
gcode_y =  87.9  + x_svg   (лево-право → возрастающий gcode_Y; левый край = наименьший Y)
```
Поэтому **`--reading-force-axis x`** (ось по умолчанию): кластеризует строки по **gcode_X** (убывающий, верхняя строка первой), внутри строки сортирует по **min(gcode_Y) → mean(gcode_Y)** (левый конец первым). Если поменять ось обратно на `y` (legacy), кластеризация разрежет текст на вертикальные ломтики вместо горизонтальных строк — полный хаос.

**Ориентация вершин:** после сортировки штрихов каждая открытая полилиния разворачивается так, чтобы её **левый конец** (меньший gcode_Y / imag) шёл первым. Скелетный трейсер обходит граф в произвольном направлении; без этого шага перо может рисовать каждый штрих справа налево.

**Linemerge:** [`scripts/build.sh`](scripts/build.sh) по умолчанию добавляет **`--skip-linemerge`**, чтобы после скелетного трейса короткие сегменты не сливались в одну полилинию с произвольным порядком вершин (сортировка чтения переставляет только целые штрихи, не точки внутри линии). Вернуть слияние (меньше pen-up): **`PDF_TO_PRINT_LINEMERGE=1`**. Ручной запуск `svg_to_gcode.py` без `--skip-linemerge` по умолчанию оставляет linemerge включённым.

**Порядок штрихов:** разрыв строк `READING_ROW_GAP_BREAK_MM` задаётся в **мм**; для геометрии vpype (внутренние единицы, px-like) порог переводится через `vp.convert_length`, для проверки по G-code используются те же мм без перевода — иначе кластеризация строк на генераторе и у валидатора расходятся.

После трансформов скрипт **собирает все слои vpype в один** (порядок как в `gwrite` — обход `document.layers`), иначе сортировка «внутри каждого слоя» не совпадала бы с одним потоком в G-code. Далее **построчно**: кластеризация центроидов по **разрыву** вдоль оси строки (`READING_ROW_GAP_BREAK_MM`, переопределение: `--reading-row-gap-mm`, env `PDF_TO_PRINT_READING_ROW_GAP_MM` / legacy `PDF_TO_PRINT_READING_ROW_BUCKET_MM`). `read --single-layer` остаётся важен для единого SVG, но дальше по пайплайну слой всё равно может размножаться. **По умолчанию ось `x`** (см. геометрию выше): переключить обратно на legacy-ось: `--reading-force-axis y` или `READING_FORCE_AXIS=y`. Чтобы снова включить старый эвристический «auto» по размаху X/Y, задайте `--reading-force-axis auto` и `PDF_TO_PRINT_READING_AXIS_AUTO=1`.

**Проверка G-code:** [`scripts/validate_reading_order_gcode.py`](scripts/validate_reading_order_gcode.py) — восстанавливает каждый штрих как полилинию (цель `G0` первой вершины + все `G1 XY` до подъёма пера), затем сверяет порядок с тем же алгоритмом permutation, что и vpype; флаг **`--strict`** дополнительно проверяет монотонность полос и порядок внутри строки. Дымовый прогон без PDF: [`scripts/e2e_reading_order_pipeline.py`](scripts/e2e_reading_order_pipeline.py). `./scripts/build.sh` выполняет e2e (Phase 0), затем после генерации страниц — **`--strict` для каждого `build/gcode/page_*.gcode`** (Phase 2b); при ошибке сборка не доходит до merge.

При запуске скрипт **перезаписывает `~/.vpype.toml`** с подставленными значениями `Z_PEN_DOWN` и `Z_HOP`. Не редактировать `~/.vpype.toml` вручную — будет затёрт при следующем запуске.

Чтобы изменить Z-калибровку:
1. Поправить `Z_PEN_DOWN` в `scripts/svg_to_gcode.py`
2. Запустить `./scripts/build.sh` — пере-сгенерируется gcode со скорректированным Z

**Почему такой transform**:
- SVG: portrait 165×205 mm, Y down (top of page = Y=0)
- `pagerotate --clockwise` (CW 90°): становится 205×165 landscape; в vpype (Y up) формула: new_x = y_portrait, new_y = W - x_portrait
- `scale -o 0 0 -- 1 -1`: Y инвертирован относительно (0,0). Итог: gcode_x = y_portrait + 50.46, gcode_y = x_svg + 87.9
- `translate` на `PAPER_ORIGIN_*`: content в области бумаги в координатах сопла (см. скрипт)

Bounds итогового G-code (pen-frame): X≈28.6..221, Y≈57..211 — внутри paper и UMTS safe zone.

### `scripts/merge_pages.py`

Конкатенирует start + (page + pause) × N + end в один G-code. `{NEXT_PAGE}` placeholder в `page_pause.gcode` заменяется на двузначный номер.

### `scripts/build.sh`

Orchestrator. Активирует .venv: Phase 0 — `e2e_reading_order_pipeline.py`; Phase 1 по умолчанию — `extract_pages_raster.sh`; Phase 2 — `svg_to_gcode.py`; Phase 2b — валидация порядка штрихов для всех страниц; Phase 3 — merge. Для прямого SVG из Inkscape — третий аргумент `vector`, либо `EXTRACT_MODE=vector` / `USE_VECTOR_EXTRACT=1` (см. комментарии в [`scripts/build.sh`](scripts/build.sh)).

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

Референс структуры `gwrite`; **фактический** `~/.vpype.toml` генерирует `svg_to_gcode.write_vpype_profile()` с подстановкой `Z_PEN_DOWN`, `Z_HOP`, `DRAW_FEED_MM_MIN`. Ключевые поля:
- `unit = "mm"` — выход в миллиметрах
- `vertical_flip = false` — ориентация Y в пайплайне (`scale 1 -1`), не в gwrite
- `segment_first` — травел (G0 F18000) + опускание (G1 Z = Z_PEN_DOWN)
- `segment` — рисование G1 XY с F из `DRAW_SPEED_MM_S` (мм/с × 60 = мм/мин)
- `line_end` — подъём до `Z_PEN_DOWN + Z_HOP` после path

**Скорости** (по умолчанию в скрипте): travel F18000 (300 мм/с); draw — см. `DRAW_SPEED_MM_S` (например 50 мм/с → F3000).

Для гелевой ручки при необходимости снизить `DRAW_SPEED_MM_S` в [`scripts/svg_to_gcode.py`](scripts/svg_to_gcode.py) и перегенерировать — не копировать шаблон вручную в `~/.vpype.toml`.

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
2. Если ручка медленнее (POSCA-style маркер) — снизить `DRAW_SPEED_MM_S` в `scripts/svg_to_gcode.py` (профиль перегенерируется в `~/.vpype.toml`)
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
- **Phase 1 raster** по умолчанию скелетон (Python); режим `TRACE_MODE=potrace` требует ImageMagick + potrace и может снова давать два контура на «кольцевых» штрихах.
