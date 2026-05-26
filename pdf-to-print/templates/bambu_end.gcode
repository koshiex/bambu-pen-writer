;===== UMTS Pen Plotter End G-code =================================
; CRITICAL: do NOT let printer auto-finish (would trigger filament cut
; sequence and collide with UMTS module). Instead pause for manual
; module removal, then operator stops the print from LCD.
;===================================================================

G1 Z{Z_TRAVEL_CLEARANCE} F{Z_TRAVEL_FEED}          ; high Z lift before pause (clearance for module + notebook)
G0 X{PARK_X} Y{PARK_Y} F{TRAVEL_FEED}   ; park at bed center for module removal
M400                  ; flush motion buffer
M73 P100 R0           ; progress 100%, complete
M400 U1               ; PAUSE: remove UMTS module, then STOP print on LCD
; (anything below runs only if operator hits Resume instead of Stop)
M104 S0               ; nozzle off
M140 S0               ; bed off
M106 S0               ; fans off
M106 P2 S0
M106 P3 S0
M84                   ; motors off
