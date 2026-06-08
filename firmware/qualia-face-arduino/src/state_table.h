// State table for the face FSM — mirrors STATE_TABLE in the CircuitPython
// source (/mnt/circuitpy/code.py).
//
// Atlas indices:
//   face:  0=center, 1=look-left, 2=look-right
//   eyes:  0=look-left, 1=look-right, 2=smile-eyes, 3=open-default,
//          4=squint, 5=sad-look, 6=small-quiet, 7=wide-surprised
//   mouth: 0=look-left-smile, 1=look-right-smile, 2=smile-big, 3=smile-small,
//          4=neutral-bar, 5=small-mouth, 6=sad-small, 7=frown-deep, 8=quiet-O,
//          9=talk-up, 10=wide-mouth, 11=scallop-talk
//
// BLINK_FALLBACK = 4 (squint as proxy until a true closed-eye sprite ships).

#pragma once
#include <stdint.h>

namespace face {

enum class State : uint8_t {
    Idle,
    Listening,
    Thinking,
    Speaking,
    Sleeping,
    Error,
    Count,
};

constexpr uint8_t BLINK_FALLBACK = 4;

struct TalkSeq {
    const uint8_t* frames;
    uint8_t length;
};

struct StateConfig {
    uint8_t face;
    uint8_t eye;
    uint8_t blink;
    uint8_t mouth;
    bool wander;
    TalkSeq talk;  // length == 0 means no talk loop
};

// Talk sequence for "speaking": cycle through these mouth frames at 6 fps.
inline constexpr uint8_t SPEAKING_TALK_FRAMES[] = {9, 10, 11, 4};

inline constexpr StateConfig STATE_TABLE[static_cast<size_t>(State::Count)] = {
    /* Idle      */ {0, 3, BLINK_FALLBACK, 4, true,  {nullptr, 0}},
    /* Listening */ {0, 2, BLINK_FALLBACK, 2, false, {nullptr, 0}},
    /* Thinking  */ {0, 5, BLINK_FALLBACK, 5, false, {nullptr, 0}},
    /* Speaking  */ {0, 3, BLINK_FALLBACK, 9, false, {SPEAKING_TALK_FRAMES, 4}},
    /* Sleeping  */ {0, 4, 4,              4, false, {nullptr, 0}},
    /* Error     */ {0, 5, BLINK_FALLBACK, 7, false, {nullptr, 0}},
};

// WANDER_EYES: (face, eye, mouth) tuples picked at random during wander state.
struct Triple {
    uint8_t face;
    uint8_t eye;
    uint8_t mouth;
};
inline constexpr Triple WANDER_EYES[] = {
    {1, 0, 0},
    {2, 1, 1},
    {0, 3, 4},
};
inline constexpr size_t WANDER_EYES_COUNT = sizeof(WANDER_EYES) / sizeof(WANDER_EYES[0]);

inline const char* stateName(State s) {
    switch (s) {
        case State::Idle:      return "idle";
        case State::Listening: return "listening";
        case State::Thinking:  return "thinking";
        case State::Speaking:  return "speaking";
        case State::Sleeping:  return "sleeping";
        case State::Error:     return "error";
        default:               return "idle";
    }
}

inline bool parseState(const String& in, State& out) {
    String s = in;
    s.trim();
    s.toLowerCase();
    if (s == "sleep") s = "sleeping";  // ALIASES = {"sleep": "sleeping"}
    if (s == "idle")      { out = State::Idle;      return true; }
    if (s == "listening") { out = State::Listening; return true; }
    if (s == "thinking")  { out = State::Thinking;  return true; }
    if (s == "speaking")  { out = State::Speaking;  return true; }
    if (s == "sleeping")  { out = State::Sleeping;  return true; }
    if (s == "error")     { out = State::Error;     return true; }
    return false;
}

}  // namespace face
