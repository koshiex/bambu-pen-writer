;===== PAGE FLIP PAUSE =====
; Operator action: open door, flip notebook page, verify alignment, close door, resume on LCD.
G1 Z50 F1200            ; high Z lift — clearance over notebook + UMTS module
G0 X128 Y200 F18000     ; park head rear-center, away from front door glass
M400                    ; wait for moves to finish
M106 S0                 ; fans off (belt-and-suspenders)
M106 P2 S0
M106 P3 S0
M73 P{PROGRESS} R{REMAINING}   ; progress update for LCD
M400 U1                 ; Bambu interactive pause — operator flips page, presses Resume on LCD
M109 S180               ; re-assert nozzle temp on resume (cooldown protection)
;===== PAGE {NEXT_PAGE} =====
;LAYER_CHANGE
;LAYER:{NEXT_PAGE}
