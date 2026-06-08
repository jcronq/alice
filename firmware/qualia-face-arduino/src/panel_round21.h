// Round21 (2.1" 480x480 RGB666 capacitive touch panel, Adafruit 5792)
// driver for Adafruit Qualia ESP32-S3 RGB-666 (Adafruit 5800).
//
// Two-stage bring-up:
//   1. PCA9554 IO expander on I2C 0x3F drives a 9-bit bit-banged SPI bus to
//      the JD9853 controller (no direct MCU SPI on this board). We use that
//      bus to send the JD9853 register init sequence — without it, the
//      panel stays in reset and the RGB lanes drive a dark screen.
//   2. After the controller is alive, bring up the ESP-IDF RGB panel driver
//      in bounce-buffer mode. Bounce buffers live in internal SRAM so the
//      LCD DMA can read at hard-real-time rates while the framebuffer in
//      PSRAM is refilled by a background task — keeps LCD pixel timing
//      stable while WiFi is hammering PSRAM.
//
// Reference (CircuitPython driver — ported into C++ below):
//   adafruit_qualia/displays/round21.py        (JD9853 init bytes)
//   shared-module/dotclockframebuffer/__init__.c (PCA9554 9-bit SPI bit-bang)
//   ports/espressif/boards/adafruit_qualia_s3_rgb666/board.c (PCA9554 wiring)

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
// Verified against the upstream Arduino-ESP32 board variant:
//   variants/adafruit_qualia_s3_rgb666/pins_arduino.h
static constexpr int PIN_DE = 2;
static constexpr int PIN_VSYNC = 42;
static constexpr int PIN_HSYNC = 41;
static constexpr int PIN_PCLK = 1;

// 16 RGB lanes: 5R + 6G + 5B. The panel ignores R0/B0 LSB on the connector
// (board ties them off), so even though the panel is RGB666 internally we
// drive it as RGB565 over the bus.
static constexpr int PIN_R[5] = {11, 10, 9, 46, 3};
static constexpr int PIN_G[6] = {48, 47, 21, 14, 13, 12};
static constexpr int PIN_B[5] = {40, 39, 38, 0, 45};

// I2C for IO expander + touch.
static constexpr int PIN_I2C_SDA = 8;
static constexpr int PIN_I2C_SCL = 18;

// PCA9554 IO expander (0x3F): drives the 9-bit SPI bus to the JD9853.
//   bit 0 = TFT_SCK            (output)
//   bit 1 = TFT_CS  (active L) (output)
//   bit 2 = TFT_RESET (active L) (output)
//   bit 3 = TP_IRQ             (input)
//   bit 4 = BACKLIGHT          (input — float high via board pull-up)
//   bit 5 = BTN_UP             (input)
//   bit 6 = BTN_DOWN           (input)
//   bit 7 = TFT_MOSI           (output)
// Direction byte 0x78 = 0b01111000 (1=input, 0=output). Matches upstream.
static constexpr uint8_t PCA9554_ADDR = 0x3F;
static constexpr uint8_t PCA_REG_OUTPUT = 0x01;
static constexpr uint8_t PCA_REG_POLARITY = 0x02;
static constexpr uint8_t PCA_REG_CONFIG = 0x03;
static constexpr uint8_t PCA_DIRECTION = 0x78;

static constexpr uint8_t SCK_MASK = 0x01;
static constexpr uint8_t CS_MASK = 0x02;
static constexpr uint8_t RESET_MASK = 0x04;
static constexpr uint8_t MOSI_MASK = 0x80;

static constexpr uint8_t CST826_ADDR = 0x15;

// Round21 timings — verified via REPL dump on hardware.
static constexpr int HSYNC_PULSE_WIDTH = 20;
static constexpr int HSYNC_BACK_PORCH = 40;
static constexpr int HSYNC_FRONT_PORCH = 40;
static constexpr int VSYNC_PULSE_WIDTH = 10;
static constexpr int VSYNC_BACK_PORCH = 40;
static constexpr int VSYNC_FRONT_PORCH = 40;

// Bounce-buffer fix: 10 scanlines worth of pixels in internal SRAM lets the
// LCD DMA read at hard-real-time speed while the framebuffer sits in PSRAM.
// Without this, WiFi PSRAM contention caused pclk underruns → column-shift
// jitter in the CircuitPython driver.
static constexpr size_t BOUNCE_BUFFER_LINES = 10;
static constexpr size_t BOUNCE_BUFFER_SIZE_PX = LCD_H_RES * BOUNCE_BUFFER_LINES;

// ---------------------------------------------------------------------------
// JD9853 register init sequence — verbatim from adafruit_qualia round21.py.
//
// Packed format (CircuitPython convention):
//   each step = [cmd_byte, len_byte, data_bytes..., (delay_ms if len_byte&0x80)]
//   len_byte bit 7 (0x80) = "delay flag" — append one delay-ms byte after
//   any data. A delay-ms byte of 0xFF is interpreted as 500ms.
// CS is held low across cmd+data, released after each step.
// ---------------------------------------------------------------------------
static const uint8_t JD9853_INIT[] = {
    // Page select: page 0x10
    0xFF, 0x05, 0x77, 0x01, 0x00, 0x00, 0x10,
    // Frame rate / display drive
    0xC0, 0x02, 0x3B, 0x00,
    0xC1, 0x02, 0x0B, 0x02,
    0xC2, 0x02, 0x00, 0x02,
    0xCC, 0x01, 0x10,
    0xCD, 0x01, 0x08,
    // Positive gamma
    0xB0, 0x10, 0x02, 0x13, 0x1B, 0x0D, 0x10, 0x05, 0x08, 0x07, 0x07, 0x24, 0x04, 0x11, 0x0E, 0x2C, 0x33, 0x1D,
    // Negative gamma
    0xB1, 0x10, 0x05, 0x13, 0x1B, 0x0D, 0x11, 0x05, 0x08, 0x07, 0x07, 0x24, 0x04, 0x11, 0x0E, 0x2C, 0x33, 0x1D,
    // Page select: page 0x11 (power / voltage)
    0xFF, 0x05, 0x77, 0x01, 0x00, 0x00, 0x11,
    0xB0, 0x01, 0x5D,
    0xB1, 0x01, 0x43,
    0xB2, 0x01, 0x81,
    0xB3, 0x01, 0x80,
    0xB5, 0x01, 0x43,
    0xB7, 0x01, 0x85,
    0xB8, 0x01, 0x20,
    0xC1, 0x01, 0x78,
    0xC2, 0x01, 0x78,
    0xD0, 0x01, 0x88,
    0xE0, 0x03, 0x00, 0x00, 0x02,
    0xE1, 0x0B, 0x03, 0xA0, 0x00, 0x00, 0x04, 0xA0, 0x00, 0x00, 0x00, 0x20, 0x20,
    0xE2, 0x0D, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0xE3, 0x04, 0x00, 0x00, 0x11, 0x00,
    0xE4, 0x02, 0x22, 0x00,
    0xE5, 0x10, 0x05, 0xEC, 0xA0, 0xA0, 0x07, 0xEE, 0xA0, 0xA0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0xE6, 0x04, 0x00, 0x00, 0x11, 0x00,
    0xE7, 0x02, 0x22, 0x00,
    0xE8, 0x10, 0x06, 0xED, 0xA0, 0xA0, 0x08, 0xEF, 0xA0, 0xA0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0xEB, 0x07, 0x00, 0x00, 0x40, 0x40, 0x00, 0x00, 0x00,
    0xED, 0x10, 0xFF, 0xFF, 0xFF, 0xBA, 0x0A, 0xBF, 0x45, 0xFF, 0xFF, 0x54, 0xFB, 0xA0, 0xAB, 0xFF, 0xFF, 0xFF,
    0xEF, 0x06, 0x10, 0x0D, 0x04, 0x08, 0x3F, 0x1F,
    // Page select: page 0x13 — set EF=0x08
    0xFF, 0x05, 0x77, 0x01, 0x00, 0x00, 0x13,
    0xEF, 0x01, 0x08,
    // Page select: page 0x00 (user / display)
    0xFF, 0x05, 0x77, 0x01, 0x00, 0x00, 0x00,
    0x36, 0x01, 0x00,            // MADCTL (orientation)
    0x3A, 0x01, 0x60,            // COLMOD: 18bpp RGB666 over the data bus
    0x11, 0x80, 0x64,            // sleep-out, then delay 100ms
    0x29, 0x80, 0x32,            // display on, then delay 50ms
};
static constexpr size_t JD9853_INIT_LEN = sizeof(JD9853_INIT);

// ---------------------------------------------------------------------------

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
    // PCA9554 / SPI bit-bang helpers.
    bool pcaWriteReg_(uint8_t reg, uint8_t val);
    bool pcaPinChange_(uint8_t set_mask, uint8_t clear_mask);
    void spiSendByte_(uint8_t b, bool is_command);

    bool initIoExpander_();
    bool resetAndInitController_();
    bool initRgbPanel_();

    esp_lcd_panel_handle_t handle_ = nullptr;
    uint16_t* fb_ = nullptr;
    uint8_t pca_shadow_ = 0;
};

// ---------------------------------------------------------------------------
// PCA9554 helpers.
// ---------------------------------------------------------------------------

inline bool Panel::pcaWriteReg_(uint8_t reg, uint8_t val) {
    Wire.beginTransmission(PCA9554_ADDR);
    Wire.write(reg);
    Wire.write(val);
    return Wire.endTransmission() == 0;
}

inline bool Panel::pcaPinChange_(uint8_t set_mask, uint8_t clear_mask) {
    pca_shadow_ = (pca_shadow_ & ~clear_mask) | set_mask;
    // Hot path: we don't want a heap-managing Wire wrapper per call — but
    // Arduino's TwoWire keeps its TX buffer static, so this is allocation-
    // free. Still, ~18 I2C transactions per byte × ~250 init bytes ≈ 90ms
    // of init time at 400kHz. One-shot at boot — acceptable.
    Wire.beginTransmission(PCA9554_ADDR);
    Wire.write(PCA_REG_OUTPUT);
    Wire.write(pca_shadow_);
    return Wire.endTransmission() == 0;
}

// 9-bit SPI MSB-first, CPOL=CPHA=0. First bit out is the DC flag: 0=command,
// 1=data. The display latches on the rising edge of CLK, so we (a) drop CLK
// low and present MOSI, then (b) raise CLK to latch.
//
// CS is held LOW through the entire byte (and across cmd+data of the whole
// init step); the caller releases CS via pcaPinChange_(CS_MASK, 0) at the
// end of the step.
inline void Panel::spiSendByte_(uint8_t b, bool is_command) {
    uint16_t bits = b;
    if (!is_command) {
        bits |= 0x100;  // DC=1 for data
    }
    for (int i = 0; i < 9; i++) {
        bool bit_high = (bits & 0x100) != 0;
        if (bit_high) {
            // MOSI high, CLK low, CS low (asserted).
            pcaPinChange_(MOSI_MASK, SCK_MASK | CS_MASK);
        } else {
            // MOSI low, CLK low, CS low (asserted).
            pcaPinChange_(0, MOSI_MASK | SCK_MASK | CS_MASK);
        }
        // Rising edge — display latches the bit.
        pcaPinChange_(SCK_MASK, 0);
        bits <<= 1;
    }
}

// ---------------------------------------------------------------------------
// Bring-up.
// ---------------------------------------------------------------------------

inline bool Panel::initIoExpander_() {
    Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL, 400000);
    Wire.setTimeOut(50);

    // PCA9554 setup matches upstream CircuitPython board.c:
    //   - direction 0x78: bits 3..6 inputs (TP_IRQ, backlight, buttons),
    //                     bits 0,1,2,7 outputs (SPI signals)
    //   - polarity inversion off
    if (!pcaWriteReg_(PCA_REG_CONFIG, PCA_DIRECTION)) {
        Serial.println("pca9554: direction write failed — IO expander not responding");
        return false;
    }
    if (!pcaWriteReg_(PCA_REG_POLARITY, 0x00)) {
        Serial.println("pca9554: polarity write failed");
        return false;
    }

    // Seed the output shadow to a known state before the bit-bang starts:
    // CS deasserted (high), RESET asserted (low), SCK low, MOSI low.
    pca_shadow_ = CS_MASK;
    if (!pcaWriteReg_(PCA_REG_OUTPUT, pca_shadow_)) {
        Serial.println("pca9554: initial output write failed");
        return false;
    }
    return true;
}

inline bool Panel::resetAndInitController_() {
    // Reset pulse: drop RESET low for 10ms, release for 100ms.
    // Then walk the JD9853 init table.
    //
    // The first pin_change here matches the CircuitPython sequence: CS high
    // (deassert), CLK low (idle), RESET low (asserted).
    if (!pcaPinChange_(CS_MASK, SCK_MASK | RESET_MASK)) return false;
    delay(10);
    if (!pcaPinChange_(RESET_MASK, 0)) return false;  // release reset
    delay(100);                                       // panel power-up

    size_t i = 0;
    while (i < JD9853_INIT_LEN) {
        if (i + 1 >= JD9853_INIT_LEN) {
            Serial.println("jd9853: truncated init table");
            return false;
        }
        uint8_t cmd = JD9853_INIT[i];
        uint8_t len_byte = JD9853_INIT[i + 1];
        bool has_delay = (len_byte & 0x80) != 0;
        uint8_t data_size = len_byte & 0x7F;

        if (i + 2 + data_size + (has_delay ? 1 : 0) > JD9853_INIT_LEN) {
            Serial.printf("jd9853: init overflow at byte %u (cmd 0x%02X)\n",
                          (unsigned)i, cmd);
            return false;
        }

        // Send the command byte (DC=0), then any data bytes (DC=1).
        spiSendByte_(cmd, true);
        for (uint8_t j = 0; j < data_size; j++) {
            spiSendByte_(JD9853_INIT[i + 2 + j], false);
        }
        // Idle CLK, deassert CS — exactly the post-step cleanup in the
        // CircuitPython dotclockframebuffer driver.
        pcaPinChange_(0, SCK_MASK);
        pcaPinChange_(CS_MASK, 0);

        size_t advance = 2 + data_size;
        if (has_delay) {
            uint8_t d = JD9853_INIT[i + 2 + data_size];
            uint16_t ms = (d == 0xFF) ? 500 : d;
            delay(ms);
            advance += 1;
        }
        i += advance;
    }
    return true;
}

inline bool Panel::initRgbPanel_() {
    esp_lcd_rgb_panel_config_t cfg = {};
    cfg.data_width = 16;          // RGB565 lanes; panel ignores R0/B0 LSB
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
        Serial.println("esp_lcd_new_rgb_panel failed");
        return false;
    }
    if (esp_lcd_panel_reset(handle_) != ESP_OK) {
        Serial.println("esp_lcd_panel_reset failed");
        return false;
    }
    if (esp_lcd_panel_init(handle_) != ESP_OK) {
        Serial.println("esp_lcd_panel_init failed");
        return false;
    }

    void* fb_void = nullptr;
    if (esp_lcd_rgb_panel_get_frame_buffer(handle_, 1, &fb_void) != ESP_OK || fb_void == nullptr) {
        Serial.println("esp_lcd_rgb_panel_get_frame_buffer failed");
        return false;
    }
    fb_ = static_cast<uint16_t*>(fb_void);
    return true;
}

inline bool Panel::begin() {
    if (!initIoExpander_()) return false;
    Serial.println("pca9554: up");
    if (!resetAndInitController_()) return false;
    Serial.println("jd9853: init sequence sent");
    if (!initRgbPanel_()) return false;
    Serial.println("rgb panel: framebuffer ready");
    return true;
}

inline void Panel::flush() {
    if (!handle_ || !fb_) return;
    // Passing the panel-owned framebuffer back to draw_bitmap triggers the
    // bounce-buffer refill cycle for the whole frame.
    esp_lcd_panel_draw_bitmap(handle_, 0, 0, LCD_H_RES, LCD_V_RES, fb_);
}

}  // namespace round21
