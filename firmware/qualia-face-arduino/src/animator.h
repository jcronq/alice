// Animator FSM — mirrors the Anim class in /mnt/circuitpy/code.py.
//
// Tick at 20 Hz (50 ms). Each tick may:
//   - cycle the talk frame (6 fps if state has a talk seq)
//   - end an in-progress blink and restore base/wander layers
//   - start a new blink (squint as proxy; held 120 ms)
//   - start a new wander (only in states with wander=true; held 1.2 s)
//   - end a wander and return to base layers
//
// All durations and jitters match the CircuitPython values verbatim.

#pragma once

#include <Arduino.h>
#include "state_table.h"

namespace face {

class Animator {
public:
    void begin();
    void setState(State s);
    State state() const { return state_; }

    // Returns true if (face, eye, mouth) tile indices changed and should be
    // redrawn this tick.
    bool tick(uint32_t now_ms, uint8_t& face_out, uint8_t& eye_out, uint8_t& mouth_out);

private:
    void scheduleBlink_(uint32_t now_ms);
    void scheduleWander_(uint32_t now_ms);
    static uint32_t randRangeMs_(uint32_t lo_ms, uint32_t hi_ms);

    State state_ = State::Idle;
    bool blinking_ = false;
    bool wandering_ = false;
    uint32_t next_blink_ms_ = 0;
    uint32_t blink_off_ms_ = 0;
    uint32_t next_wander_ms_ = 0;
    uint32_t wander_off_ms_ = 0;
    uint32_t last_talk_ms_ = 0;
    Triple wander_layers_ = {0, 3, 4};
    uint8_t talk_idx_ = 0;
    uint8_t cur_face_ = 0;
    uint8_t cur_eye_ = 3;
    uint8_t cur_mouth_ = 4;
};

}  // namespace face
