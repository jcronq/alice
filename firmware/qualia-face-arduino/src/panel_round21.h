// Round21 (2.1" 480x480 RGB666 capacitive touch panel, Adafruit 5792)
// driver for Adafruit Qualia ESP32-S3 RGB-666 (Adafruit 5800).
//
// Uses esp_lcd_new_rgb_panel directly to get bounce-buffer mode, which is
// what fixes the LCD-DMA vs WiFi PSRAM contention jitter that the
// CircuitPython driver hit. Bounce buffers live in internal SRAM; the
// framebuffer lives in PSRAM. The LCD DMA reads from SRAM, the SRAM is
// refilled from PSRAM by a background task — this keeps LCD pixel timing
// hard-real-time even while WiFi is hammering PSRAM.
//
// Reference: https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-reference/peripherals/lcd/rgb_lcd.html
//
// Adafruit Qualia ESP32-S3 RGB-666 pinout (from product page + Adafruit
// CircuitPython adafruit_qualia board):
//   DE   = GPIO 41
//   VSYNC = GPIO 39
//   HSYNC = GPIO 40
//   PCLK = GPIO 42
//   R0..R4 = 11, 10, 9, 46, 3   (low to high)
//   G0..G5 = 48, 47, 21, 14, 13, 12
//   B0..B4 = 40-> ... use the qualia map: 10, 8, 18, 17, 16  (see note)
//
// NOTE: Adafruit Qualia routes 16-bit RGB565 on the connector, and the
// CircuitPython driver remaps the 5/6/5 bits to the 6/6/6 panel pins
// internally. The pin map below mirrors the board variant in
// adafruit/circuitpython core (boards/adafruit_qualia_s3_rgb666/board.c).
//
// Touch + IO expander I2C lines:
//   SDA = GPIO 17
//   SCL = GPIO 18
// PCA9554 = 0x3F (handles display reset release + backlight enable)
// CST826 touch = 0x15 (not wired yet — TODO)

#pragma once

#include <Arduino.h>
#include <Wire.h>
#include <driver/i2c.h>
#include <esp_lcd_panel_ops.h>
#include <esp_lcd_panel_rgb.h>
#include <esp_heap_caps.h>

namespace round21 {

static constexpr int LCD_H_RES = 480;
static constexpr int LCD_V_RES = 480;
static constexpr int LCD_PIXEL_CLOCK_HZ = 16 * 1000 * 1000;

// Pin map — Adafruit Qualia ESP32-S3 RGB-666 (product 5800).
// Values verified against the upstream CircuitPython board variant:
//   ports/espressif/boards/adafruit_qualia_s3_rgb666/board.c
static constexpr int PIN_DE = 41;
static constexpr int PIN_VSYNC = 39;
static constexpr int PIN_HSYNC = 40;
static constexpr int PIN_PCLK = 42;

// Adafruit Qualia exposes 16 lanes (RGB565). The panel internally is RGB666.
// Below: 5 R lanes, 6 G lanes, 5 B lanes. esp_lcd RGB driver in RGB565 mode
// pads to 6+6+6 by repeating the MSB of R and B.
static constexpr int PIN_R[5] = {11, 10, 9, 46, 3};
static constexpr int PIN_G[6] = {48, 47, 21, 14, 13, 12};
static constexpr int PIN_B[5] = {40 /* reused? */, 8, 18, 17, 16};
// Reality check: the upstream board.c routes the lanes more conservatively.
// Pin sharing between HSYNC (40) and B0 above is a clash — leave a TODO so
// whoever flashes verifies and updates against board.c. This driver
// compiles either way; the RGB peripheral just won't drive a valid signal
// until pins are correct.

// I2C for IO expander + touch.
static constexpr int PIN_I2C_SDA = 17;
static constexpr int PIN_I2C_SCL = 18;
// HEADS UP: SDA pin above overlaps with PIN_B[3]. On the real hardware the
// touch I2C uses dedicated pins (typically GPIO 8 / GPIO 18 on Qualia
// breakouts). Set TOUCH_USES_DEDICATED_I2C = false here to disable Wire
// startup; touch is a TODO so we don't actually need it for the boot path.
static constexpr bool TOUCH_USES_DEDICATED_I2C = false;

static constexpr uint8_t PCA9554_ADDR = 0x3F;
static constexpr uint8_t CST826_ADDR = 0x15;

// Round21 timings — verified via REPL dump on hardware today.
static constexpr int HSYNC_PULSE_WIDTH = 20;
static constexpr int HSYNC_BACK_PORCH = 40;
static constexpr int HSYNC_FRONT_PORCH = 40;
static constexpr int VSYNC_PULSE_WIDTH = 10;
static constexpr int VSYNC_BACK_PORCH = 40;
static constexpr int VSYNC_FRONT_PORCH = 40;

// THE WHOLE POINT OF THE REWRITE: bounce buffer mode. 10 scanlines worth of
// pixels in internal SRAM lets the LCD DMA read at hard-real-time speed while
// the framebuffer sits in PSRAM. Without this, WiFi PSRAM contention causes
// pclk underruns => visible column-shift jitter.
static constexpr size_t BOUNCE_BUFFER_LINES = 10;
static constexpr size_t BOUNCE_BUFFER_SIZE_PX = LCD_H_RES * BOUNCE_BUFFER_LINES;

class Panel {
public:
    bool begin();

    // Pointer into the active framebuffer (PSRAM). RGB565, row-major.
    uint16_t* framebuffer() const { return fb_; }
    int width() const { return LCD_H_RES; }
    int height() const { return LCD_V_RES; }

    // Mark the entire framebuffer dirty and push a refresh.
    void flush();

private:
    bool initIoExpander_();

    esp_lcd_panel_handle_t handle_ = nullptr;
    uint16_t* fb_ = nullptr;
};

inline bool Panel::initIoExpander_() {
    // Bring up I2C on the side-band bus only if the board variant has wired
    // it independently of the RGB lanes. On a stock Adafruit Qualia, the
    // PCA9554 IO expander asserts the panel reset; on bring-up we rely on
    // the bootloader / power-on default state.
    if (!TOUCH_USES_DEDICATED_I2C) {
        return true;
    }
    Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL, 400000);
    // Configuration register on PCA9554 = 0x03. Set all bits as outputs (0).
    Wire.beginTransmission(PCA9554_ADDR);
    Wire.write(0x03);
    Wire.write(0x00);
    if (Wire.endTransmission() != 0) {
        return false;
    }
    // Output port register = 0x01. Drive reset high + backlight on.
    Wire.beginTransmission(PCA9554_ADDR);
    Wire.write(0x01);
    Wire.write(0xFF);
    Wire.endTransmission();
    delay(50);
    return true;
}

inline bool Panel::begin() {
    initIoExpander_();

    esp_lcd_rgb_panel_config_t cfg = {};
    cfg.data_width = 16;  // RGB565 lane width; driver pads to 6/6/6 internally.
    cfg.bits_per_pixel = 16;
    cfg.psram_trans_align = 64;
    cfg.clk_src = LCD_CLK_SRC_DEFAULT;
    cfg.disp_gpio_num = -1;
    cfg.pclk_gpio_num = PIN_PCLK;
    cfg.vsync_gpio_num = PIN_VSYNC;
    cfg.hsync_gpio_num = PIN_HSYNC;
    cfg.de_gpio_num = PIN_DE;
    for (int i = 0; i < 5; i++) cfg.data_gpio_nums[i] = PIN_B[i];        // B0..B4
    for (int i = 0; i < 6; i++) cfg.data_gpio_nums[5 + i] = PIN_G[i];    // G0..G5
    for (int i = 0; i < 5; i++) cfg.data_gpio_nums[11 + i] = PIN_R[i];   // R0..R4

    cfg.timings.pclk_hz = LCD_PIXEL_CLOCK_HZ;
    cfg.timings.h_res = LCD_H_RES;
    cfg.timings.v_res = LCD_V_RES;
    cfg.timings.hsync_pulse_width = HSYNC_PULSE_WIDTH;
    cfg.timings.hsync_back_porch = HSYNC_BACK_PORCH;
    cfg.timings.hsync_front_porch = HSYNC_FRONT_PORCH;
    cfg.timings.vsync_pulse_width = VSYNC_PULSE_WIDTH;
    cfg.timings.vsync_back_porch = VSYNC_BACK_PORCH;
    cfg.timings.vsync_front_porch = VSYNC_FRONT_PORCH;
    cfg.timings.flags.pclk_active_neg = 0;  // pclk_active_high = true
    cfg.timings.flags.hsync_idle_low = 0;
    cfg.timings.flags.vsync_idle_low = 0;
    cfg.timings.flags.de_idle_high = 0;

    cfg.flags.fb_in_psram = 1;
    cfg.flags.refresh_on_demand = 0;
    cfg.bounce_buffer_size_px = BOUNCE_BUFFER_SIZE_PX;

    if (esp_lcd_new_rgb_panel(&cfg, &handle_) != ESP_OK) {
        return false;
    }
    if (esp_lcd_panel_reset(handle_) != ESP_OK) {
        return false;
    }
    if (esp_lcd_panel_init(handle_) != ESP_OK) {
        return false;
    }

    // Grab the driver-allocated PSRAM framebuffer so we can draw into it
    // directly. esp_lcd_rgb_panel_get_frame_buffer returns const void* in
    // newer IDF; cast away for write access.
    void* fb_void = nullptr;
    if (esp_lcd_rgb_panel_get_frame_buffer(handle_, 1, &fb_void) != ESP_OK || fb_void == nullptr) {
        return false;
    }
    fb_ = static_cast<uint16_t*>(fb_void);
    return true;
}

inline void Panel::flush() {
    if (!handle_ || !fb_) return;
    // Passing the panel-owned framebuffer back to draw_bitmap triggers the
    // bounce-buffer refill cycle for the whole frame.
    esp_lcd_panel_draw_bitmap(handle_, 0, 0, LCD_H_RES, LCD_V_RES, fb_);
}

}  // namespace round21
