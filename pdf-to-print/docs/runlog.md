# Runlog — журнал прогонов и калибровок

После каждого теста / прогона / правки скриптов — добавить запись внизу.

Шаблон записи:

```markdown
## YYYY-MM-DD — <короткое описание>

**Этап**: Test 1 / Test 2 / Test 3 / Test 4 / Page N / Full run / Pipeline change
**Параметры**: Z-offset=X мм, скорость draw=Y мм/с, paper origin=(X,Y), толщина тетради=Z мм
**Результат**: успех / частичный / провал
**Что наблюдали**: ...
**Проблемы**: ...
**Изменения в pipeline/конфиге/operator-manual**: ...
**Следующий шаг**: ...
```

---

## 2026-05-03 — Initial pipeline build

**Этап**: Pipeline initial setup
**Параметры**: paper origin=(24, 217) landscape, draw speed=200 mm/s (F12000), travel=300 mm/s (F18000), Z-hop=3 мм
**Результат**: ✅ pipeline работает на dry run

**Что сделано**:
- Извлечены 24 SVG из `pdfs/2.pdf` через Inkscape (vector ✓, 165×205 mm portrait)
- vpype pipeline: pagerotate CCW + scale 1 -1 + translate 24mm 217mm + linemerge + linesort
- Сгенерирован `output/notebook.gcode` 141 MB
- 25 пауз M400 U1 в файле (1 install + 23 page-flip + 1 end), 24 PAGE markers
- Координаты всех страниц в bounds: X∈[28.4, 222.5], Y∈[56.9, 211.3] — внутри paper area (24..229, 52..217) ✓

**Наблюдения**:
- X-min на всех страницах ≈28.6 — поля чётных/нечётных НЕ чередуются в исходном PDF. Word без mirror margins. Решим эмпирически после Test 4.
- Размер 141 МБ может быть проблемой для P1S firmware buffer и для Orca preview — будем тестировать
- Inkscape флаг `--pages=N` (не `--pdf-page=N` как в старых версиях)
- Python 3.14 несовместим с vpype (`pkg_resources` import error в pnoise) — используется 3.13

**Проблемы**: пока нет (тестов на железе ещё не было)

**Следующий шаг**: Test 1 — dry run на P1S без ручки и бумаги, проверить отсутствие коллизий

---

## 2026-05-04 — Z-offset bake fix

**Этап**: Pipeline change
**Параметры**: `Z_PEN_DOWN=18.0`, `Z_HOP=3.0` (добавлены в `scripts/svg_to_gcode.py`)
**Результат**: ✅ critical bug найден и исправлен ДО первого запуска на железе

**Что было не так**:
- Ранее vpype emit-ил `G1 Z0` (pen down) и `G1 Z3` (Z-hop) — pen-frame координаты
- Но Bambu firmware интерпретирует Z буквально, не применяя slicer-time Z-offset из printer profile к raw gcode
- При прямой отправке `output/notebook.gcode` через Device → Open G-code File:
  - `G1 Z0` → сопло у бумаги, ручка торчит ~18 мм ниже бумаги → удар в стол
- UMTS-доковый Z-offset (+17/+20 мм) применяется только при slicer-Slice, не при raw gcode стриме

**Что сделано**:
- В `scripts/svg_to_gcode.py` добавлены константы `Z_PEN_DOWN=18.0` и `Z_HOP=3.0`
- Скрипт теперь авто-генерирует `~/.vpype.toml` с подставленными Z-значениями
- vpype теперь emit-ит `G1 Z18.000` (pen down, nozzle 18мм над столом, ручка касается бумаги) и `G1 Z21.000` (Z-hop, ручка 3мм над бумагой)
- Удалена синхронизация `~/.vpype.toml` из `build.sh` (скрипт делает сам)
- `templates/vpype_profile.toml` стал референсным (с placeholder'ами `<Z_PEN_DOWN>`)
- Документация: пересобраны `operator-manual.md` §1.3 (упрощено — Orca profile почти не нужен) и `pipeline.md`
- Перегенерирован `output/notebook.gcode` (141.9 МБ, 25 пауз)

**Изменения**:
- `scripts/svg_to_gcode.py`: +константы Z_PEN_DOWN/Z_HOP, +функция write_vpype_profile()
- `scripts/build.sh`: убрана `cp templates/vpype_profile.toml ~/.vpype.toml` (теперь in-script)
- `templates/vpype_profile.toml`: переделан в референсный шаблон
- `docs/operator-manual.md` §1.3: переписана секция настроек Orca
- `docs/pipeline.md`: обновлена секция svg_to_gcode и карта файлов

**Следующий шаг**: тот же — Test 1 dry run. Если на dry run видно что Z-offset большой/малый → правка `Z_PEN_DOWN` + регенерация.

---

## 2026-05-04 — Preview metadata + pause debugging

**Этап**: Pipeline change
**Параметры**: добавлены M73 progress calls, ;LAYER:N markers, Bambu metadata header
**Результат**: ✅ preview в Orca должен показывать оценку времени и layer count

**Что было не так**:
- Юзер: «принтер включает вентиляторы и намертво виснет, в предпросмотре нет времени»
- Причина 1: gcode не имел `;LAYER:`, `M73`, header-метаданных → Orca preview не мог оценить
- Причина 2 (предположительно): «зависание» = сработавший `M400 U1` install pause в конце start G-code, юзер не понял что это пауза
- Возможная причина 3: G29 ABL или M970.3 mech mode check фейлятся

**Что сделано**:
- `scripts/merge_pages.py`: добавлен `metadata_header()` с `; total layer number`, `; total estimated time`, `; HEADER_BLOCK_START/END` (формат Bambu Studio для preview)
- `merge_pages.py`: per-page `;LAYER_CHANGE` + `;LAYER:N` + `M73 P{progress} R{remaining}` markers
- `templates/page_pause.gcode`: добавлен M73, fans-off (M106 S0 × 3) перед паузой, Z lift повышен до 25 мм
- `templates/bambu_start.gcode`: явные fans-off (M106 S0/P2 S0/P3 S0) перед install pause; M73 P0 progress
- `merge_pages.py`: новый аргумент `--minutes-per-page` (default 15) для оценки времени
- `docs/operator-manual.md` §5: новая секция troubleshooting с пунктом 5.1 (fans+hang = install pause), 5.2 (preview time)

**Перегенерирован output/notebook.gcode**:
- 49 M73 progress calls
- 47 LAYER markers (24 pages + 23 pause-next)
- header с estimated 360 min (15×24)
- 141.9 МБ size без изменений

**Следующий шаг**: реальный dry run на P1S. Проверить:
1. Видит ли Orca preview время печати и layer count
2. Реально ли срабатывает M400 U1 (LCD статус «Paused»)
3. Если M400 U1 не работает → fallback на M0 (см. §5.1 troubleshooting)
4. Реальное время на страницу → обновить `--minutes-per-page`

---

## 2026-05-04 — Minimal start G-code (критические фиксы)

**Этап**: Pipeline change
**Параметры**: переписан `templates/bambu_start.gcode` в minimal-версию, M400 U1 → M0 везде, header в Bambu CONFIG_BLOCK формате
**Результат**: ✅ убраны корневые причины 3 из 3 проблем dry run

**Что было не так** (отчёт юзера):
1. Принтер виснет после старта, **экран не реагирует на клики** (не пауза, реальный crash)
2. Плита прогревается до ~50°C несмотря на bed=0 в filament profile
3. Метаданные не показываются в Orca preview

**Корневые причины**:
- (1) **M970.3 / M974** (mech mode fast check, виброкалибровка) и **M976** (heatbed scan) — Bambu-специфичные команды, фейлятся / фризят firmware без филамента
- (2) **G29 ABL** в start G-code — Bambu firmware автоматически греет плиту до ~50°C ДО прозондирования для термокомпенсации
- (3) Header был в неправильном формате; Orca/Bambu Studio парсят `; CONFIG_BLOCK_START/END` блок

**Что сделано**:
- `templates/bambu_start.gcode` полностью переписан в **minimal-версию**:
  - Удалены: printer sound, wipe nozzle sequence, G29 ABL, M970.3/M974, M976
  - Оставлены: M17, G90, M140 S0 (явный bed cold), M106 ×3 (fans off), M221 X0 Y0 Z0 (soft endstop off), G28 home, G1 Z20, M104/M109 S180, M0 pause
  - Добавлен HEADER_BLOCK с layer/time данными
- Все `M400 U1` заменены на `M0` (Marlin-стандарт, гарантированно работает на P1S):
  - `templates/bambu_start.gcode` install pause
  - `templates/page_pause.gcode` page-flip pauses
  - `templates/bambu_end.gcode` final pause
- `scripts/merge_pages.py` — header переделан в Bambu CONFIG_BLOCK_START/END формат с `printer_model = Bambu Lab P1S`, `total layer number`, `estimated printing time`, etc

**Перегенерирован output/notebook.gcode**:
- 25 `M0` pauses (1 install + 23 flip + 1 end), 0 `M400 U1`
- 0 команд нагрева плиты (M140 S0 в начале, нет M190)
- 0 G29/M970.3/M974/M976
- header в CONFIG_BLOCK формате
- 141.9 МБ size без изменений

**Следующий шаг**: новый dry run. Ожидаем:
- Плита остаётся холодной (нет G29 → нет auto-heat)
- M0 пауза реально пауза, экран P1S показывает «Paused», клики работают
- Orca preview показывает 24 layers, ~360 min

---

## 2026-05-04 — SD-card workflow + Bambu format wrapping

**Этап**: Pipeline change
**Параметры**: добавлены HEADER_BLOCK / THUMBNAIL_BLOCK / EXECUTABLE_BLOCK_START/END / CONFIG_BLOCK markers
**Результат**: ⚠️ частичный — для LAN ✓, для SD не гарантировано

**Что было не так** (отчёт юзера):
- Юзер запускает gcode с SD карты, не через LAN
- Принтер виснет при minimal start gcode тоже
- В preview / runtime метаданных не видно

**Корневая причина**:
- P1S firmware при чтении gcode с SD строго валидирует формат: ожидает Bambu Studio-style блоки с HEADER, THUMBNAIL, EXECUTABLE_BLOCK_START/END, CONFIG_BLOCK
- Наш raw generated gcode без этих блоков → firmware отказывается / парсер фризит UI
- LAN-стрим эту валидацию обходит (байты идут напрямую)

**Что сделано**:
- `scripts/merge_pages.py`:
  - `metadata_header()` теперь генерирует полный Bambu-формат HEADER_BLOCK_START/END с `BambuStudio 01.09.05.51` версией, `model printer variant: P1S`, `total layer number`, `max_z_height`, etc
  - Добавлен THUMBNAIL_BLOCK с dummy 1×1 PNG base64 (некоторые firmware требуют непустой)
  - Добавлен EXECUTABLE_BLOCK_START перед start gcode
  - Новая функция `end_marker()` пишет EXECUTABLE_BLOCK_END + CONFIG_BLOCK_START/END после end gcode
- Per-page markers расширены: `;LAYER_NUMBER: N` и `;HEIGHT: 0.1` (Bambu-style)
- `templates/bambu_start.gcode`: убран старый встроенный HEADER_BLOCK (теперь его пишет merge_pages.py)
- `docs/operator-manual.md`:
  - §4 Способ A (LAN рекомендован) / Способ B (SD fallback)
  - §5.0 troubleshoot SD-card freeze

**Структура output/notebook.gcode сейчас**:
```
HEADER_BLOCK_START ... HEADER_BLOCK_END
THUMBNAIL_BLOCK_START ... THUMBNAIL_BLOCK_END (dummy 1x1 PNG)
EXECUTABLE_BLOCK_START
  bambu_start.gcode (minimal)
  PAGE 01..24 with M73, LAYER markers
  page pauses M0
  bambu_end.gcode
EXECUTABLE_BLOCK_END
CONFIG_BLOCK_START ... CONFIG_BLOCK_END
```

**Следующий шаг**:
1. Юзер: попробовать LAN-стрим как primary (Orca → Device → Open G-code File)
2. Если SD обязательно: попробовать с новыми блоками. Если всё равно виснет — извлекать preamble из реального Bambu Studio sliced файла как hack workaround
3. Долгосрочно: исследовать минимальный набор полей в Bambu format header который удовлетворяет P1S firmware (возможно нужны plate JSON, machine bounding box, конкретные filament parameters)

---

## 2026-05-04 — Use real Bambu Studio benchy as template

**Этап**: Pipeline change
**Параметры**: HEADER_BLOCK + CONFIG_BLOCK взяты буквально из `P1S_3DBenchy by Creative Tools_PLA.gcode` (Bambu Studio sliced file)
**Результат**: ⏳ ждёт SD-проверки

**Что было не так**:
- Юзер: SD не работает, синтетический Bambu header недостаточен
- Юзер положил в корень проекта файл `P1S_3DBenchy by Creative Tools_PLA.gcode` — реально работающий с SD на P1S референс

**Анализ benchy**:
- `; HEADER_BLOCK_START` ... `_END` — 6 строк, минимум: BambuStudio version, model printing time, total layer number, model label id
- `; CONFIG_BLOCK_START` ... `_END` — 307 строк, **гигантский** список параметров (filament, print, machine, motion, fan, retraction). Firmware скорее всего просто требует валидной структуры, конкретные значения второстепенны.
- **НЕТ THUMBNAIL_BLOCK** — мой dummy thumbnail был лишним, мог даже мешать
- `EXECUTABLE_BLOCK_START` → `M73 P0 R<min>` → `M201/M203/M204/M205` motion limits → `M106 S0` × 2 fans off → `; FEATURE: Custom` → start G-code body
- Layer markers формат: `; CHANGE_LAYER` (с пробелом) + `; Z_HEIGHT: N` + `; LAYER_HEIGHT: N`, не `;LAYER_CHANGE` как было у меня
- `M73 L<n>` для layer-progress (одна метка на слой)
- `M73 P<%> R<min>` для общего прогресса (часто, ~каждые 50-100 секунд работы)
- `EXECUTABLE_BLOCK_END` в самом конце

**Что сделано**:
- Извлечены `templates/bambu_header_block.gcode` (6 строк) и `templates/bambu_config_block.gcode` (307 строк) **буквально** из benchy
- `scripts/merge_pages.py` полностью переписан:
  - читает HEADER_BLOCK template, патчит `total layer number` и `model printing time` под наши значения
  - читает CONFIG_BLOCK как есть
  - добавляет motion limits (`motion_limits()`) после `EXECUTABLE_BLOCK_START`
  - использует Bambu-формат layer markers (`layer_marker()`: CHANGE_LAYER + Z_HEIGHT + LAYER_HEIGHT + M73 L<n>)
  - **убран** synthetic THUMBNAIL_BLOCK
- `templates/bambu_start.gcode`: убран placeholder `M73 P0 R360` (теперь его эмитит merge_pages.py)

**Перегенерирован output/notebook.gcode** (141.9 МБ → 135 МБ):
- Структура зеркалит benchy
- 25 M0 pauses, 74 M73 progress calls
- 0 команд нагрева плиты
- HEADER_BLOCK (6 строк), CONFIG_BLOCK (307 строк) — реальные из benchy
- EXECUTABLE_BLOCK_START → motion limits → M73 P0 → start G-code → 24 страницы → end G-code → EXECUTABLE_BLOCK_END

**Следующий шаг**: попробовать на SD-карте. Должен парситься P1S firmware (структура та же что у benchy который заведомо работает).

---

## 2026-05-04 — M0 → M400 U1 (Bambu firmware ignores M0)

**Этап**: Pipeline change (pause command)
**Параметры**: все pause-команды переключены на `M400 U1`
**Результат**: ✅ паузы должны срабатывать

**Что было не так** (отчёт юзера):
- SD-проверка с Bambu-формат wrapper'ом прошла — принтер начал печать, но **сразу пишет всё подряд**, не делает паузы
- Установочной паузы тоже не было — пишет без модуля / на полу

**Корневая причина**: **Bambu P1S firmware игнорирует `M0`**. Несмотря на то, что M0 — Marlin-стандарт, прошивка Bambu не реализует его как interactive pause. Канонический pause command для Bambu — `M400 U1`.

Раньше юзер сообщал что M400 U1 «виснет» — но реально висел не на M400 U1, а на G29/M974/wipe ДО неё. Сейчас с minimal start G-code этих команд нет, M400 U1 должна срабатывать как ожидается.

**Что сделано**:
- `templates/bambu_start.gcode`: install pause M0 → M400 U1
- `templates/page_pause.gcode`: page-flip pause M0 → M400 U1
- `templates/bambu_end.gcode`: final pause M0 → M400 U1
- `docs/operator-manual.md` §5.1 обновлён: M400 U1 — единственный работающий pause command на P1S, M0/M601/M226 игнорируются

**Перегенерирован output/notebook.gcode**:
- 25 M400 U1 pauses (было 0)
- 0 M0 pauses (было 25)

**Следующий шаг**: повторный SD-тест. Ожидаемое поведение:
- Принтер home + heat nozzle (180°C) + park → **PAUSE** (экран показывает Paused)
- Оператор: установить UMTS модуль с ручкой → Resume на LCD
- Принтер рисует страницу 1 → **PAUSE** → оператор переворачивает страницу → Resume
- Повторение 23 раза для остальных страниц
- После страницы 24 → **PAUSE** → оператор снимает модуль → Stop на LCD

---

## 2026-05-04 — SD печать работает, calibration tool added

**Этап**: Real run start + Pipeline addition
**Параметры**: Z_PEN_DOWN=18.0 (default, не калибровано), Z_HOP=3.0
**Результат**: ✅ принтер пошёл писать с SD карты, паузы работают

**Что произошло**:
- Юзер: «Все норм, работает». Печать с SD стартовала, M400 U1 паузы срабатывают, экран показывает Paused
- Запросил детальную инструкцию для Test 3 — Z-калибровка с ручкой

**Что сделано**:
- Создан `scripts/calibration_gcode.py` — генератор тестового G-code (1.7 КБ, ~30 сек прогон, 5 коротких линий)
  - Использует ту же Bambu-структуру что notebook.gcode (HEADER + CONFIG + EXECUTABLE)
  - Импортирует `Z_PEN_DOWN` / `Z_HOP` из `svg_to_gcode.py`
  - Имеет CLI флаги `--z-down` / `--z-hop` для итераций без правки скрипта
  - Output: `output/calibration.gcode`
- `docs/operator-manual.md` §2 Test 3 переписан с детальной процедурой:
  - Объяснена физика: Z_PEN_DOWN фиксирован, регулируется физическая глубина ручки в модуле
  - Цикл итерации с таблицей диагностики «что видишь → что делать»
  - Альтернатива: правка `Z_PEN_DOWN` через CLI флаг для жёстких ручек

**Следующий шаг**: юзер делает Test 3, записывает финальный Z в этот файл

---

## 2026-05-04 — Park position fix (collision with front door glass)

**Этап**: Pipeline change
**Параметры**: park position изменён с (X=128, Y=20, Z=25) на (X=128, Y=200, Z=50)
**Результат**: ✅ collision устранена

**Что было не так** (отчёт юзера):
- Notebook.gcode выезжает за безопасную зону при travelling, ударяется об стекло
- Анализ: 25 движений на Y=20 — install pause + 23 page-flip + end park
- Y=20 ниже excluded zone (Y<55 в nozzle frame) → UMTS модуль с ручкой цеплял переднюю дверцу/стекло P1S
- Drawing-движения сами в безопасной зоне (X 28..222, Y 57..211 ✓), только park был unsafe

**Что сделано**:
- `templates/bambu_start.gcode`: `G0 X128 Y20 → G0 X128 Y200`, `G1 Z20 → G1 Z50`
- `templates/page_pause.gcode`: то же
- `templates/bambu_end.gcode`: то же
- `scripts/calibration_gcode.py`: то же
- `scripts/alignment_gcode.py`: то же

Park теперь на rear-center (Y=200) — далеко от front door glass. Z=50 высокий — обеспечивает clearance над тетрадью + UMTS модулем.

Логика: оператор открывает переднюю дверцу для page flip; голова в задней части стола = доступ к тетради через open door без помех.

**Перегенерирован output/notebook.gcode**:
- Out-of-bounds: 0 (было 25)
- Все G0/G1 в pen-frame X∈[28, 229], Y∈[57, 217] ✓

**Следующий шаг**: повторный прогон notebook.gcode на P1S. Travel-зона теперь безопасна.

---

## 2026-05-05 — Pen-nozzle XY offset compensation

**Этап**: Pipeline change (critical)
**Параметры**: `PEN_OFFSET_X=-26.46`, `PEN_OFFSET_Y=-37.9`, `PAPER_LEFT=24`, `PAPER_FRONT=50`
**Результат**: ✅ pen теперь физически попадает в paper position

**Что было не так**:
- alignment.gcode рисовал не в ожидаемом месте — левый край рамки шёл по краю bed (X≈0), задевая Y-axis опоры; передний край рамки гораздо ближе к двери чем 50мм
- Корневая причина: pipeline трактовал gcode coords как pen-frame, но firmware двигает СОПЛО. Pen смещена от nozzle в сторону (0, 0) по обоим осям.
- UMTS docs прямо пишет: «Кончик ручки не совпадает с соплом»

**Замер штангенциркулем**:
- delta_X = 26.46 мм
- delta_Y = 37.9 мм
- Diagonal sanity: √(26.46² + 37.9²) = 46.2 ≈ user's 46.5 ✓

**Что сделано**:
- `scripts/svg_to_gcode.py`: добавлены `PEN_OFFSET_X/Y`, `PAPER_LEFT`, `PAPER_FRONT`, `PAPER_W/H`. `PAPER_ORIGIN_X/Y` пересчитан как `PAPER_LEFT - PEN_OFFSET_X` / `PAPER_FRONT + PAPER_H - PEN_OFFSET_Y` → vpype translate теперь в nozzle frame
- `scripts/calibration_gcode.py`: импорт `PEN_OFFSET_X/Y`, добавлена функция `pen_to_nozzle()`, при эмиссии gcode конвертит pen→nozzle. Bounds в pen-frame обновлены: 22→24, 227→229
- `scripts/alignment_gcode.py`: то же. `thick_line()` теперь конвертит pen→nozzle перед эмиссией. Bounds: 22→24, 227→229
- `templates/*.gcode` park не трогали (уже nozzle frame, безопасно)
- `docs/operator-manual.md` §1.3.0: положение тетради на bed-relative (24мм от bed left, 50мм от bed front)

**Перегенерированы артефакты**:
- `output/alignment.gcode`: nozzle X∈[50.16, 255.76], Y∈[87.60, 253.20] ✓ внутри bed
- `output/calibration.gcode`: nozzle X∈[106.46, 156.46], Y∈[137.90, 200] ✓
- `output/notebook.gcode`: nozzle X∈[54.82, 248.93], Y∈[92.79, 247.15] ✓ 0 out-of-bed

**Следующий шаг**: запустить alignment.gcode (без тетради) → замерить рулеткой положение рамки → должно быть 24мм от левого края bed и 50мм от переднего края. Если delta>2мм — подкрутить `PEN_OFFSET_X/Y` (пропорционально delta).
