import time
import math
import board
import digitalio
import terminalio
import os
import pwmio
from adafruit_magtag.magtag import MagTag

# ==============================================================================
# Hardware Constants and Colors
# ==============================================================================
COLOR_OFF = (0, 0, 0)
COLOR_ORANGE = (255, 60, 0)   # Focus mode color
COLOR_TEAL = (0, 180, 255)    # Break mode color
COLOR_RED = (255, 0, 0)       # Focus setup indicator
COLOR_GREEN = (0, 255, 0)     # Break setup indicator

# Initialize the MagTag hardware helper
magtag = MagTag()
magtag.peripherals.neopixels.brightness = 0.1  # Low brightness to prevent blinding

# ==============================================================================
# UI Display Configuration
# ==============================================================================
# 2.9" E-ink screen is 296x128. We add 6 dynamic text labels.
# Label indices are determined by the order they are added.

# Index 0: Section Title
magtag.add_text(
    text_font=terminalio.FONT,
    text_position=(148, 20),
    text_scale=1,
    text_anchor_point=(0.5, 0.5),
)

# Index 1: Main Big Text (Time / Status)
magtag.add_text(
    text_font=terminalio.FONT,
    text_position=(148, 62),
    text_scale=2,
    text_anchor_point=(0.5, 0.5),
)

# Index 2: Button A Label (above physical Button A, centered at x=37)
magtag.add_text(
    text_font=terminalio.FONT,
    text_position=(37, 115),
    text_scale=1,
    text_anchor_point=(0.5, 0.5),
)

# Index 3: Button B Label (above physical Button B, centered at x=111)
magtag.add_text(
    text_font=terminalio.FONT,
    text_position=(111, 115),
    text_scale=1,
    text_anchor_point=(0.5, 0.5),
)

# Index 4: Button C Label (above physical Button C, centered at x=185)
magtag.add_text(
    text_font=terminalio.FONT,
    text_position=(185, 115),
    text_scale=1,
    text_anchor_point=(0.5, 0.5),
)

# Index 5: Button D Label (above physical Button D, centered at x=259)
magtag.add_text(
    text_font=terminalio.FONT,
    text_position=(259, 115),
    text_scale=1,
    text_anchor_point=(0.5, 0.5),
)

# ==============================================================================
# Global State Variables
# ==============================================================================
state = "IDLE"                  # IDLE, SETUP_FOCUS, SETUP_BREAK, RUNNING_FOCUS, RUNNING_BREAK

# Load default durations from settings.toml (CircuitPython 10 style) or fallback to standards
try:
    focus_time = int(os.getenv("FOCUS_TIME", 30))
except (ValueError, TypeError):
    focus_time = 30

try:
    break_time = int(os.getenv("BREAK_TIME", 10))
except (ValueError, TypeError):
    break_time = 10

setup_val = focus_time          # Temporary adjustable value used during setup
setup_modified = False          # Tracks if setup screen needs a deferred E-ink refresh
last_button_activity = 0.0      # Timestamp of last Button C or D press for deferred refresh
timer_start_time = 0.0          # Monotonic start timestamp of the active timer
last_min_displayed = 0          # Last minute count shown on screen (prevents redundant refreshes)
cycle_count = 0                 # Number of completed Focus + Break cycles

# Keep track of previous button states for edge-detection debouncing
# active-low: True is released, False is pressed
btn_prev = [True] * len(magtag.peripherals.buttons)

def play_custom_tone(frequency, duration):
    """Play a pure tone on the speaker pin using direct, robust PWMOut."""
    if frequency <= 0:
        return
    
    # 1. Turn on the speaker amplifier via MagTag's existing reference
    try:
        magtag.peripherals._speaker_enable.value = True
    except AttributeError:
        pass
    
    # Give the hardware amplifier chip 10ms to wake up from shutdown
    time.sleep(0.01)
    
    # 2. Output PWM tone
    try:
        # 50% duty cycle (32768) generates a solid, clean tone
        with pwmio.PWMOut(board.SPEAKER, duty_cycle=32768, frequency=int(frequency), variable_frequency=True) as speaker_pwm:
            time.sleep(duration)
    except Exception as e:
        print(f"Error playing tone: {e}")
    finally:
        # 3. Disable speaker amplifier when done to save power
        try:
            magtag.peripherals._speaker_enable.value = False
        except AttributeError:
            pass

# ==============================================================================
# Soundscape Player
# ==============================================================================
def play_sound(sound_type):
    """Play customized audio soundscapes using MagTag's speaker."""
    if sound_type == "click":
        # Short instant click feedback (increased to 0.08s to allow amplifier turn-on time)
        play_custom_tone(1200, 0.08)
    elif sound_type == "cancel":
        # Descending cancel chime
        play_custom_tone(1000, 0.1)
        time.sleep(0.05)
        play_custom_tone(600, 0.15)
    elif sound_type == "focus_complete":
        # Ascending arpeggio played twice to alert end of focus
        for _ in range(2):
            for freq in (1500, 2000, 2500):
                play_custom_tone(freq, 0.1)
                time.sleep(0.03)
            time.sleep(0.1)
    elif sound_type == "break_complete":
        # Sweet dual chime signaling return to focus
        play_custom_tone(1200, 0.18)
        time.sleep(0.08)
        play_custom_tone(1800, 0.22)

# ==============================================================================
# Visual & UI Update Helpers
# ==============================================================================
def set_labels(title, main, btn_a="", btn_b="", btn_c="", btn_d=""):
    """Set the text for all screen labels in memory (does not refresh E-ink)."""
    magtag.set_text(title, index=0, auto_refresh=False)
    magtag.set_text(main, index=1, auto_refresh=False)
    magtag.set_text(btn_a, index=2, auto_refresh=False)
    magtag.set_text(btn_b, index=3, auto_refresh=False)
    magtag.set_text(btn_c, index=4, auto_refresh=False)
    magtag.set_text(btn_d, index=5, auto_refresh=False)

def update_running_neopixels(progress, color):
    """Update NeoPixels to show timer progress with a pulsing active LED."""
    # Oscillate brightness of the active LED using math.sin (non-blocking)
    # Brightness will oscillate between 0.02 and 0.18
    pulse = 0.02 + 0.16 * (math.sin(time.monotonic() * 4.5) + 1.0) / 2.0
    
    # Determine the number of fully lit LEDs
    completed_leds = int(progress * 4)
    completed_leds = min(3, max(0, completed_leds))
    
    for i in range(4):
        if i < completed_leds:
            # Solid finished LEDs: steady dim intensity
            magtag.peripherals.neopixels[i] = tuple(int(c * 0.08) for c in color)
        elif i == completed_leds:
            # Current LED: pulse to show active count-down
            magtag.peripherals.neopixels[i] = tuple(int(c * pulse) for c in color)
        else:
            # Future LEDs: Off
            magtag.peripherals.neopixels[i] = COLOR_OFF

# ==============================================================================
# State Entry Handlers (Configuring Screen Content & NeoPixels)
# ==============================================================================
def enter_idle():
    """Transition to IDLE state."""
    global cycle_count
    magtag.peripherals.neopixels.fill(COLOR_OFF)
    
    if cycle_count > 0:
        main_text = f"Ready\nFocus: {focus_time}m | Break: {break_time}m\nCycles: {cycle_count}"
    else:
        main_text = f"Ready\nFocus: {focus_time}m | Break: {break_time}m"
        
    set_labels(
        title="=== FOCUS TIMER ===",
        main=main_text,
        btn_a="[START]",
        btn_b="[SETUP]",
        btn_c="",
        btn_d=""
    )
    magtag.refresh()

def enter_setup_focus():
    """Transition to SETUP_FOCUS state."""
    magtag.peripherals.neopixels.fill(COLOR_OFF)
    # Light up first two LEDs red to signal focus setup
    magtag.peripherals.neopixels[0] = COLOR_RED
    magtag.peripherals.neopixels[1] = COLOR_RED
    
    set_labels(
        title="=== SETUP MODE ===",
        main=f"Set Focus Time\n-> {setup_val} mins <-",
        btn_a="[CANCEL]",
        btn_b="[NEXT]",
        btn_c="[  +  ]",
        btn_d="[  -  ]"
    )
    magtag.refresh()

def enter_setup_break():
    """Transition to SETUP_BREAK state."""
    magtag.peripherals.neopixels.fill(COLOR_OFF)
    # Light up last two LEDs green to signal break setup
    magtag.peripherals.neopixels[2] = COLOR_GREEN
    magtag.peripherals.neopixels[3] = COLOR_GREEN
    
    set_labels(
        title="=== SETUP MODE ===",
        main=f"Set Break Time\n-> {setup_val} mins <-",
        btn_a="[CANCEL]",
        btn_b="[SAVE]",
        btn_c="[  +  ]",
        btn_d="[  -  ]"
    )
    magtag.refresh()

def enter_running_focus():
    """Transition to RUNNING_FOCUS state."""
    global last_min_displayed, cycle_count
    last_min_displayed = focus_time
    set_labels(
        title="=== FOCUSING ===",
        main=f"Session {cycle_count + 1}\n{focus_time} mins left",
        btn_a="[STOP]",
        btn_b="",
        btn_c="",
        btn_d=""
    )
    magtag.refresh()

def enter_running_break():
    """Transition to RUNNING_BREAK state."""
    global last_min_displayed, cycle_count
    last_min_displayed = break_time
    set_labels(
        title="=== BREAK TIME ===",
        main=f"Session {cycle_count + 1}\n{break_time} mins left",
        btn_a="[STOP]",
        btn_b="",
        btn_c="",
        btn_d=""
    )
    magtag.refresh()

# ==============================================================================
# Button Click Handler
# ==============================================================================
def handle_button_press(index):
    """Process actions when a button is clicked based on the current state."""
    global state, focus_time, break_time, setup_val, setup_modified, last_button_activity, timer_start_time, cycle_count
    
    # index: 0 = A, 1 = B, 2 = C, 3 = D
    if state == "IDLE":
        if index == 0:  # Button A: START
            play_sound("click")
            state = "RUNNING_FOCUS"
            timer_start_time = time.monotonic()
            enter_running_focus()
        elif index == 1:  # Button B: SETUP
            play_sound("click")
            state = "SETUP_FOCUS"
            setup_val = focus_time
            enter_setup_focus()
            
    elif state == "SETUP_FOCUS":
        if index == 0:  # Button A: CANCEL
            play_sound("cancel")
            state = "IDLE"
            setup_modified = False
            enter_idle()
        elif index == 1:  # Button B: NEXT (Confirm Focus, move to Break Setup)
            play_sound("click")
            focus_time = setup_val
            state = "SETUP_BREAK"
            setup_val = break_time
            setup_modified = False
            enter_setup_break()
        elif index == 2:  # Button C: UP (+)
            play_sound("click")
            setup_val = min(99, setup_val + 1)
            setup_modified = True
            last_button_activity = time.monotonic()
            set_labels(
                title="=== SETUP MODE ===",
                main=f"Set Focus Time\n-> {setup_val} mins <-",
                btn_a="[CANCEL]", btn_b="[NEXT]", btn_c="[  +  ]", btn_d="[  -  ]"
            )
        elif index == 3:  # Button D: DOWN (-)
            play_sound("click")
            setup_val = max(1, setup_val - 1)
            setup_modified = True
            last_button_activity = time.monotonic()
            set_labels(
                title="=== SETUP MODE ===",
                main=f"Set Focus Time\n-> {setup_val} mins <-",
                btn_a="[CANCEL]", btn_b="[NEXT]", btn_c="[  +  ]", btn_d="[  -  ]"
            )
            
    elif state == "SETUP_BREAK":
        if index == 0:  # Button A: CANCEL
            play_sound("cancel")
            state = "IDLE"
            setup_modified = False
            enter_idle()
        elif index == 1:  # Button B: SAVE (Confirm Break, exit setup)
            play_sound("click")
            break_time = setup_val
            state = "IDLE"
            setup_modified = False
            play_sound("break_complete")
            enter_idle()
        elif index == 2:  # Button C: UP (+)
            play_sound("click")
            setup_val = min(99, setup_val + 1)
            setup_modified = True
            last_button_activity = time.monotonic()
            set_labels(
                title="=== SETUP MODE ===",
                main=f"Set Break Time\n-> {setup_val} mins <-",
                btn_a="[CANCEL]", btn_b="[SAVE]", btn_c="[  +  ]", btn_d="[  -  ]"
            )
        elif index == 3:  # Button D: DOWN (-)
            play_sound("click")
            setup_val = max(1, setup_val - 1)
            setup_modified = True
            last_button_activity = time.monotonic()
            set_labels(
                title="=== SETUP MODE ===",
                main=f"Set Break Time\n-> {setup_val} mins <-",
                btn_a="[CANCEL]", btn_b="[SAVE]", btn_c="[  +  ]", btn_d="[  -  ]"
            )
            
    elif state in ("RUNNING_FOCUS", "RUNNING_BREAK"):
        if index == 0:  # Button A: STOP
            play_sound("cancel")
            cycle_count = 0
            state = "IDLE"
            enter_idle()

# ==============================================================================
# Initialization & Main Loop
# ==============================================================================
# Start up the UI in IDLE mode
enter_idle()

while True:
    now = time.monotonic()
    
    # 1. Edge-detection for physical buttons (A, B, C, D)
    for idx, btn in enumerate(magtag.peripherals.buttons):
        val = btn.value
        if not val and btn_prev[idx]:  # Pressed (active-low transition True -> False)
            handle_button_press(idx)
        btn_prev[idx] = val
        
    # Re-query monotonic time before task execution to avoid stale time measurements
    # from blocking E-ink refreshes inside button presses
    now = time.monotonic()
        
    # 2. State-specific background task execution
    if state == "SETUP_FOCUS" or state == "SETUP_BREAK":
        # Deferred E-ink refresh handler:
        # Trigger E-ink refresh ONLY when user stops clicking for 1.0s
        if setup_modified and (now - last_button_activity >= 1.0):
            # The screen content is already set in memory, trigger hardware refresh
            magtag.refresh()
            setup_modified = False
            
    elif state == "RUNNING_FOCUS":
        elapsed = max(0.0, now - timer_start_time)
        total_sec = focus_time * 60
        
        if elapsed >= total_sec:
            # Focus session completed!
            play_sound("focus_complete")
            state = "RUNNING_BREAK"
            timer_start_time = time.monotonic()
            enter_running_break()
        else:
            # 1. Dynamic NeoPixel progress pulsing/filling
            update_running_neopixels(elapsed / total_sec, COLOR_ORANGE)
            
            # 2. Minute-by-minute E-ink screen refresh
            remaining_sec = max(0, total_sec - elapsed)
            remaining_min = math.ceil(remaining_sec / 60)
            if remaining_min != last_min_displayed:
                set_labels(
                    title="=== FOCUSING ===",
                    main=f"Session {cycle_count + 1}\n{remaining_min} mins left",
                    btn_a="[STOP]"
                )
                magtag.refresh()
                last_min_displayed = remaining_min
                
    elif state == "RUNNING_BREAK":
        elapsed = max(0.0, now - timer_start_time)
        total_sec = break_time * 60
        
        if elapsed >= total_sec:
            # Break session completed!
            play_sound("break_complete")
            cycle_count += 1
            state = "RUNNING_FOCUS"
            timer_start_time = time.monotonic()
            enter_running_focus()
        else:
            # 1. Dynamic NeoPixel progress pulsing/filling
            update_running_neopixels(elapsed / total_sec, COLOR_TEAL)
            
            # 2. Minute-by-minute E-ink screen refresh
            remaining_sec = max(0, total_sec - elapsed)
            remaining_min = math.ceil(remaining_sec / 60)
            if remaining_min != last_min_displayed:
                set_labels(
                    title="=== BREAK TIME ===",
                    main=f"Session {cycle_count + 1}\n{remaining_min} mins left",
                    btn_a="[STOP]"
                )
                magtag.refresh()
                last_min_displayed = remaining_min
                
    # Small pause to maintain high responsiveness and clean NeoPixel pulses
    time.sleep(0.02)
