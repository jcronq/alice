# qualia-face-arduino

Arduino-ESP32 port of the Alice face renderer for the Adafruit Qualia
ESP32-S3 RGB-666 (product 5800) + 2.1" Round21 480x480 RGB666 panel
(product 5792).

## Why this rewrite exists

The original CircuitPython implementation at
`/mnt/circuitpy/code.py` (on the Pi `cronqj@10.20.30.170`) drove the panel
fine until WiFi came up. As soon as the radio started hammering PSRAM, the
LCD DMA — which was reading pixels directly from PSRAM — started losing
its real-time guarantee on the pixel clock. The visible symptom was
column-shift jitter on every WiFi-induced PSRAM contention burst.

ESP-IDF's RGB panel driver has a "bounce buffer" mode that fixes exactly
this: pixels are staged through a small ring of buffers in internal SRAM,
and a background task refills them from PSRAM. The LCD DMA only ever
reads from SRAM, so WiFi traffic to PSRAM no longer affects pixel timing.
CircuitPython's displayio doesn't expose this knob — Arduino + ESP-IDF
do.

The bounce-buffer config lives in `src/panel_round21.h`:

```cpp
cfg.flags.fb_in_psram = 1;
cfg.bounce_buffer_size_px = LCD_H_RES * 10;  // 10 scanlines = 4800 px
```

10 scanlines was chosen to comfortably exceed the worst-case PSRAM stall
duration observed under WiFi load. Each bounce buffer is allocated in
internal SRAM by the driver, so the budget cost is ~9.6 KB per buffer x 2.

`CONFIG_SPIRAM_FETCH_INSTRUCTIONS` and `CONFIG_SPIRAM_RODATA` are enabled
via `build_flags` (the SPIRAM XIP family). This lets the CPU fetch code
and rodata from PSRAM, so flash I/O doesn't stall the main loop while we
push frames.

## Layout

```
firmware/qualia-face-arduino/
  platformio.ini
  src/
    main.cpp              setup/loop, compositor, WiFi, HTTP server
    panel_round21.h       esp_lcd_new_rgb_panel config + bounce buffer mode
    state_table.h         STATE_TABLE constants (mirrors CircuitPython)
    animator.h            FSM declarations
    animator.cpp          blink/wander/talk tick logic
    sprites.h             RGB565 sprite atlas declarations
    sprites.cpp           PROGMEM RGB565 pixel arrays (generated)
  tools/
    convert_sprites.py    Pillow-based BMP -> C header generator
    sprites_src/
      face.bmp            108x36, 3 tiles of 36x36 (idx 1 = transparent)
      eyes.bmp            136x8, 8 tiles of 17x8   (idx 0 = transparent)
      mouths.bmp          204x8, 12 tiles of 17x8  (idx 0 = transparent)
  README.md
```

## Build & flash

```sh
cd firmware/qualia-face-arduino

# (One-off) regenerate sprites from BMPs.
python3 tools/convert_sprites.py

# Build.
pio run

# Flash + monitor.
pio run -t upload
pio device monitor
```

Boot logs report `panel up: 480x480 (PSRAM fb + bounce buffer)` followed
by WiFi association and the HTTP listener line.

## HTTP API

Bound to port `8080`.

| method | path     | body                                  | response                                   |
|--------|----------|---------------------------------------|--------------------------------------------|
| GET    | `/`      | —                                     | `{"state": "...", "status": "...", "ip": "..."}` |
| POST   | `/state` | `idle` or `{"state": "idle"}`         | `{"state": "idle"}` or `400 bad`           |
| POST   | `/status`| free-form text or `{"status": "..."}` | `{"status": "..."}`                        |

Valid states: `idle`, `listening`, `thinking`, `speaking`, `sleeping`,
`error`. Alias: `sleep` → `sleeping`.

## Tooling versions

- PlatformIO core: 6.x
- `platform-espressif32`: pinned `^6.5.0` (ESP-IDF v5.1)
- `arduino-esp32`: bundled with platform-espressif32 (Arduino v2.0.14+)
- LovyanGFX: not currently used — the bounce-buffer-capable RGB panel API
  is the IDF call path; we draw straight into the framebuffer with a hand-
  rolled compositor. Adding LovyanGFX as a drawing API on top of the same
  PSRAM framebuffer is a future optimization.

## TODOs

- Wire CST826 touch (0x15 over I2C) — not used yet, deferred to the next pass.
- Verify the PCA9554 IO expander init sequence against the Adafruit Qualia
  variant board file. Right now we rely on bootloader defaults having
  released the panel reset.
- Verify RGB pin assignments in `panel_round21.h` against
  `boards/adafruit_qualia_s3_rgb666/board.c` in adafruit-circuitpython —
  the comment in that header flags the SDA pin collision with `PIN_B[3]`
  that needs to be reconciled on real hardware. Compile passes either way;
  the RGB panel won't drive a clean signal until pins match the board.
- Pin LovyanGFX as the drawing API once `Panel_RGB::cfg.bounce_buffer_size`
  is exposed stably.
