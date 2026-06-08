#include "animator.h"

namespace face {

// Timing constants — units in ms (CircuitPython used seconds).
static constexpr uint32_t TICK_MS = 50;            // 20 Hz tick
static constexpr uint32_t TALK_PERIOD_MS = 1000 / 6;
static constexpr uint32_t BLINK_HOLD_MS = 120;
static constexpr uint32_t BLINK_MIN_MS = 3500;
static constexpr uint32_t BLINK_MAX_MS = 6000;
static constexpr uint32_t WANDER_HOLD_MS = 1200;
static constexpr uint32_t WANDER_MIN_MS = 4000;
static constexpr uint32_t WANDER_MAX_MS = 8000;

uint32_t Animator::randRangeMs_(uint32_t lo_ms, uint32_t hi_ms) {
    return lo_ms + (random() % (hi_ms - lo_ms + 1));
}

void Animator::scheduleBlink_(uint32_t now_ms) {
    next_blink_ms_ = now_ms + randRangeMs_(BLINK_MIN_MS, BLINK_MAX_MS);
}

void Animator::scheduleWander_(uint32_t now_ms) {
    next_wander_ms_ = now_ms + randRangeMs_(WANDER_MIN_MS, WANDER_MAX_MS);
}

void Animator::begin() {
    uint32_t now = millis();
    scheduleBlink_(now);
    scheduleWander_(now);
    const StateConfig& cfg = STATE_TABLE[static_cast<size_t>(state_)];
    cur_face_ = cfg.face;
    cur_eye_ = cfg.eye;
    cur_mouth_ = cfg.mouth;
}

void Animator::setState(State s) {
    state_ = s;
    // Reset talk loop position so a fresh state starts cleanly.
    talk_idx_ = 0;
    last_talk_ms_ = 0;
}

bool Animator::tick(uint32_t now_ms, uint8_t& face_out, uint8_t& eye_out, uint8_t& mouth_out) {
    const StateConfig& cfg = STATE_TABLE[static_cast<size_t>(state_)];
    uint8_t new_face = cur_face_, new_eye = cur_eye_, new_mouth = cur_mouth_;

    auto emit = [&](uint8_t f, uint8_t e, uint8_t m) {
        new_face = f;
        new_eye = e;
        new_mouth = m;
    };

    // Talk loop short-circuits other animations.
    if (cfg.talk.length > 0 && cfg.talk.frames != nullptr) {
        if (last_talk_ms_ == 0 || (now_ms - last_talk_ms_) >= TALK_PERIOD_MS) {
            last_talk_ms_ = now_ms;
            talk_idx_ = (talk_idx_ + 1) % cfg.talk.length;
        }
        emit(cfg.face, cfg.eye, cfg.talk.frames[talk_idx_]);
    } else if (blinking_) {
        if ((int32_t)(now_ms - blink_off_ms_) >= 0) {
            blinking_ = false;
            scheduleBlink_(now_ms);
            if (wandering_) {
                emit(wander_layers_.face, wander_layers_.eye, wander_layers_.mouth);
            } else {
                emit(cfg.face, cfg.eye, cfg.mouth);
            }
        }
        // While still blinking, keep prior emit values.
    } else if ((int32_t)(now_ms - next_blink_ms_) >= 0) {
        blinking_ = true;
        blink_off_ms_ = now_ms + BLINK_HOLD_MS;
        emit(cur_face_, cfg.blink, cur_mouth_);
    } else if (cfg.wander) {
        if (wandering_) {
            if ((int32_t)(now_ms - wander_off_ms_) >= 0) {
                wandering_ = false;
                scheduleWander_(now_ms);
                emit(cfg.face, cfg.eye, cfg.mouth);
            }
            // Else keep wander layers — already set on activation.
        } else if ((int32_t)(now_ms - next_wander_ms_) >= 0) {
            wandering_ = true;
            wander_off_ms_ = now_ms + WANDER_HOLD_MS;
            wander_layers_ = WANDER_EYES[random() % WANDER_EYES_COUNT];
            emit(wander_layers_.face, wander_layers_.eye, wander_layers_.mouth);
        } else if (cur_face_ != cfg.face || cur_eye_ != cfg.eye || cur_mouth_ != cfg.mouth) {
            emit(cfg.face, cfg.eye, cfg.mouth);
        }
    } else if (cur_face_ != cfg.face || cur_eye_ != cfg.eye || cur_mouth_ != cfg.mouth) {
        emit(cfg.face, cfg.eye, cfg.mouth);
    }

    bool changed = (new_face != cur_face_) || (new_eye != cur_eye_) || (new_mouth != cur_mouth_);
    cur_face_ = new_face;
    cur_eye_ = new_eye;
    cur_mouth_ = new_mouth;
    face_out = cur_face_;
    eye_out = cur_eye_;
    mouth_out = cur_mouth_;
    return changed;
}

}  // namespace face
