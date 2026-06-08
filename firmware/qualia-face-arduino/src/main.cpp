// Alice face — 3-layer compositing rig for the Adafruit Qualia ESP32-S3
// RGB-666 + Round21 panel. Port of /mnt/circuitpy/code.py with the LCD-DMA
// vs WiFi PSRAM contention fixed by ESP-IDF bounce buffer mode.
//
// Pipeline:
//   1. Animator FSM ticks at 20 Hz, emits (face_idx, eye_idx, mouth_idx)
//      whenever any of them change.
//   2. Compositor blits the three sprite tiles into a 36x36 staging buffer
//      (face palette transparent idx skipped, then eyes+mouth painted on top
//      with their own transparent idx skipped), then scales 10x into the
//      480x480 PSRAM framebuffer.
//   3. esp_lcd_panel_draw_bitmap pushes the framebuffer via the RGB panel.
//      With bounce_buffer_size_px = 480*10, the LCD DMA is fed from internal
//      SRAM, so WiFi traffic to PSRAM no longer starves the pixel clock.
//
// HTTP API (port 8080):
//   GET  /         -> {"state": "...", "status": "...", "ip": "..."}
//   POST /state    -> body = "idle" | {"state": "idle"} (set FSM state)
//   POST /status   -> body = "..."   | {"status": "..."} (free-form status)

#include <Arduino.h>
#include <WiFi.h>
#include <ESPAsyncWebServer.h>
#include <ArduinoJson.h>

#include "panel_round21.h"
#include "sprites.h"
#include "state_table.h"
#include "animator.h"

namespace {

constexpr char WIFI_SSID[] = "Net5";
constexpr char WIFI_PASSWORD[] = "JPfarm1959";
constexpr uint16_t HTTP_PORT = 8080;
constexpr uint32_t TICK_MS = 50;
constexpr int SCALE = 10;

round21::Panel panel;
face::Animator animator;
AsyncWebServer server(HTTP_PORT);

String g_status = "";
String g_ip = "0.0.0.0";

// 36x36 staging buffer — the composite of face + eyes + mouth before scale.
uint16_t composite_buf[36 * 36];

uint16_t readTilePixel(const uint16_t* atlas,
                       uint16_t atlas_w,
                       uint16_t tile_w,
                       uint16_t tile_h,
                       uint8_t tile_idx,
                       uint16_t x,
                       uint16_t y) {
    // Atlas is laid out as a single row of tiles. Tile origin in atlas:
    //   src_x = tile_idx * tile_w
    //   src_y = 0
    uint16_t src_x = tile_idx * tile_w + x;
    uint16_t src_y = y;
    return atlas[src_y * atlas_w + src_x];
}

void compositeIntoStaging(uint8_t face_idx, uint8_t eye_idx, uint8_t mouth_idx) {
    // Face fills the staging buffer; transparent pixels stay as their fill (BG = black).
    constexpr uint16_t BG = 0x0000;
    for (uint16_t y = 0; y < FACE_TILE_H; y++) {
        for (uint16_t x = 0; x < FACE_TILE_W; x++) {
            uint16_t p = readTilePixel(FACE_PIXELS, FACE_W, FACE_TILE_W, FACE_TILE_H,
                                       face_idx, x, y);
            composite_buf[y * FACE_TILE_W + x] = (p == SPRITE_TRANSPARENT) ? BG : p;
        }
    }
    // Eyes on top, offset (EYE_REL_X, EYE_REL_Y) inside the face tile.
    for (uint16_t y = 0; y < EYES_TILE_H; y++) {
        for (uint16_t x = 0; x < EYES_TILE_W; x++) {
            uint16_t p = readTilePixel(EYES_PIXELS, EYES_W, EYES_TILE_W, EYES_TILE_H,
                                       eye_idx, x, y);
            if (p == SPRITE_TRANSPARENT) continue;
            uint16_t dx = EYE_REL_X + x;
            uint16_t dy = EYE_REL_Y + y;
            if (dx < FACE_TILE_W && dy < FACE_TILE_H) {
                composite_buf[dy * FACE_TILE_W + dx] = p;
            }
        }
    }
    // Mouth on top, offset (MOUTH_REL_X, MOUTH_REL_Y).
    for (uint16_t y = 0; y < MOUTHS_TILE_H; y++) {
        for (uint16_t x = 0; x < MOUTHS_TILE_W; x++) {
            uint16_t p = readTilePixel(MOUTHS_PIXELS, MOUTHS_W, MOUTHS_TILE_W, MOUTHS_TILE_H,
                                       mouth_idx, x, y);
            if (p == SPRITE_TRANSPARENT) continue;
            uint16_t dx = MOUTH_REL_X + x;
            uint16_t dy = MOUTH_REL_Y + y;
            if (dx < FACE_TILE_W && dy < FACE_TILE_H) {
                composite_buf[dy * FACE_TILE_W + dx] = p;
            }
        }
    }
}

void blitScaledToFramebuffer() {
    uint16_t* fb = panel.framebuffer();
    if (!fb) return;
    const int W = panel.width();
    const int H = panel.height();
    const int face_w_scaled = FACE_TILE_W * SCALE;
    const int face_h_scaled = FACE_TILE_H * SCALE;
    const int origin_x = (W - face_w_scaled) / 2;
    const int origin_y = (H - face_h_scaled) / 2;

    for (int sy = 0; sy < FACE_TILE_H; sy++) {
        for (int sx = 0; sx < FACE_TILE_W; sx++) {
            uint16_t color = composite_buf[sy * FACE_TILE_W + sx];
            int dy_base = origin_y + sy * SCALE;
            int dx_base = origin_x + sx * SCALE;
            for (int dy_off = 0; dy_off < SCALE; dy_off++) {
                int dy = dy_base + dy_off;
                if (dy < 0 || dy >= H) continue;
                uint16_t* row = fb + dy * W;
                for (int dx_off = 0; dx_off < SCALE; dx_off++) {
                    int dx = dx_base + dx_off;
                    if (dx < 0 || dx >= W) continue;
                    row[dx] = color;
                }
            }
        }
    }
}

void fillFramebufferBlack() {
    uint16_t* fb = panel.framebuffer();
    if (!fb) return;
    const int total = panel.width() * panel.height();
    for (int i = 0; i < total; i++) fb[i] = 0x0000;
}

void connectWiFi() {
    Serial.printf("wifi: connecting to %s\n", WIFI_SSID);
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    uint32_t start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < 30000) {
        delay(250);
        Serial.print(".");
    }
    if (WiFi.status() == WL_CONNECTED) {
        g_ip = WiFi.localIP().toString();
        Serial.printf("\nwifi: %s\n", g_ip.c_str());
    } else {
        Serial.println("\nwifi: timeout, continuing offline");
    }
}

String stringFromBody(AsyncWebServerRequest* request, uint8_t* data, size_t len, const char* key) {
    String body;
    body.reserve(len + 1);
    for (size_t i = 0; i < len; i++) body += (char)data[i];
    body.trim();
    if (body.length() > 0 && body[0] == '{') {
        JsonDocument doc;
        if (deserializeJson(doc, body) == DeserializationError::Ok) {
            const char* v = doc[key];
            if (v) return String(v);
        }
    }
    return body;
}

void registerRoutes() {
    server.on("/", HTTP_GET, [](AsyncWebServerRequest* req) {
        JsonDocument doc;
        doc["state"] = face::stateName(animator.state());
        doc["status"] = g_status;
        doc["ip"] = g_ip;
        String out;
        serializeJson(doc, out);
        req->send(200, "application/json", out);
    });

    server.on("/state", HTTP_POST,
              [](AsyncWebServerRequest* req) { /* empty — handled in body cb */ },
              nullptr,
              [](AsyncWebServerRequest* req, uint8_t* data, size_t len, size_t /*idx*/, size_t /*total*/) {
                  String value = stringFromBody(req, data, len, "state");
                  face::State s;
                  if (!face::parseState(value, s)) {
                      req->send(400, "text/plain", String("bad: ") + value + "\n");
                      return;
                  }
                  animator.setState(s);
                  JsonDocument doc;
                  doc["state"] = face::stateName(s);
                  String out;
                  serializeJson(doc, out);
                  req->send(200, "application/json", out);
              });

    server.on("/status", HTTP_POST,
              [](AsyncWebServerRequest* req) {},
              nullptr,
              [](AsyncWebServerRequest* req, uint8_t* data, size_t len, size_t /*idx*/, size_t /*total*/) {
                  g_status = stringFromBody(req, data, len, "status");
                  JsonDocument doc;
                  doc["status"] = g_status;
                  String out;
                  serializeJson(doc, out);
                  req->send(200, "application/json", out);
              });
}

}  // namespace

void setup() {
    Serial.begin(115200);
    // wait for USB CDC enumeration so debug output and host comms come up
    // before we do anything that might block (I2C, panel init).
    uint32_t t0 = millis();
    while (!Serial && (millis() - t0) < 3000) delay(50);
    delay(200);
    Serial.println("\nalice-face: boot");

    if (!panel.begin()) {
        Serial.println("panel.begin() failed — halting");
        while (true) delay(1000);
    }
    Serial.printf("panel up: %dx%d (PSRAM fb + bounce buffer)\n", panel.width(), panel.height());

    fillFramebufferBlack();

    randomSeed((uint32_t)esp_random());
    animator.begin();

    // Paint the initial idle face before bringing up WiFi.
    uint8_t f, e, m;
    animator.tick(millis(), f, e, m);
    compositeIntoStaging(f, e, m);
    blitScaledToFramebuffer();
    panel.flush();

    connectWiFi();
    registerRoutes();
    server.begin();
    Serial.printf("http: listening on %s:%u\n", g_ip.c_str(), HTTP_PORT);
}

void loop() {
    static uint32_t last = 0;
    uint32_t now = millis();
    if (now - last < TICK_MS) {
        delay(2);
        return;
    }
    last = now;

    uint8_t f, e, m;
    bool changed = animator.tick(now, f, e, m);
    if (changed) {
        compositeIntoStaging(f, e, m);
        blitScaledToFramebuffer();
        panel.flush();
    }
}
