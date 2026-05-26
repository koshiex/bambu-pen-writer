;===== UMTS Pen Plotter MINIMAL Start G-code (P1S) ===================
; Stripped-down version — original UMTS start G-code from docs/umts-p1s-pen.md
; caused freezes (G29 ABL, M970.3/M974 mech mode check, wipe sequence don't
; play well with empty nozzle / pen-plot mode on certain firmware versions).
;
; What's kept:
;   - Soft endstop off (UMTS requirement, allows pen reach areas outside nozzle limit)
;   - Nozzle heated to 180°C (prevents "cold extrusion prevention" error)
;   - Bed cold (M140 S0 explicit — no auto-heat from leveling)
;   - All fans off
;   - Home XYZ
;   - Park + pause for module install
;
; What's removed (all caused freezes / unnecessary for pen plot):
;   - Printer sound block (M1006...) — cosmetic
;   - Wipe nozzle sequence — needed only for filament priming, breaks with empty nozzle
;   - G29 ABL bed leveling — paper / magnet sheet height differs, may timeout
;   - M970.3 / M974 mech mode fast check (vibration calibration) — fails without filament
;   - M976 heatbed scan — not needed
;=====================================================================
; (HEADER_BLOCK + CONFIG_BLOCK + EXECUTABLE_BLOCK_START + motion limits
;  prepended by merge_pages.py — derived from real Bambu Studio benchy gcode)

;===== reset machine state =================
M17                       ; enable all steppers
G90                       ; absolute positioning
M83                       ; relative E moves (no extrusion anyway)
M220 S100                 ; reset feed rate
M221 S100                 ; reset flow rate

;===== ensure cold bed (no leveling auto-heat) =================
M140 S0                   ; bed temp 0
M141 S0                   ; chamber temp 0 (if supported)

;===== fans completely off =================
M106 S0                   ; part cooling fan
M106 P2 S0                ; aux fan
M106 P3 S0                ; chamber fan

;===== disable soft endstops (UMTS requirement) =================
M221 X0 Y0 Z0             ; turn off X/Y/Z soft endstops

;===== home all axes =================
G28                       ; home XYZ
G1 Z{Z_TRAVEL_CLEARANCE} F{Z_TRAVEL_FEED}               ; raise Z — clearance for module install + over notebook

;===== heat nozzle to 180°C (prevents cold extrusion error) =================
M104 S180                 ; nozzle target 180
M109 S180                 ; wait for nozzle to reach 180

;===== install pause: load module with pen now =================
; Park at bed center — easier reach through front door than rear Y=200.
G0 X{PARK_X} Y{PARK_Y} F{TRAVEL_FEED}
M400                      ; flush motion buffer
M400 U1                   ; PAUSE: install UMTS module + pen, then press Resume on LCD
                          ; (M0 is ignored on Bambu P1S firmware — must use M400 U1)
;===== drawing begins after Resume =================================
